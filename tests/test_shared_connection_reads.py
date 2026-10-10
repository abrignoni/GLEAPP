"""Threads that share a case's connection each get the row they asked for.

Every thread of the app shares one connection per case: the request threads, a
job, the snapshot loop and the Find similar indexer. From Python 3.12 the sqlite3
module's statement cache can give two threads that run the same statement text
the same prepared statement, and one then reads the answer to the other's
question. ``sharedreads.py`` asks for rows from several threads while another
keeps the connection busy, here through ``CaseDB.get_file`` and through
``/api/file/<id>``, and every answer has to be the row asked for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import sharedreads                                  # pylint: disable=import-error
from gleapp import db as dbmod
from gleapp.case import open_case

ROOT = Path(__file__).resolve().parents[1]

ROWS = 2000
THREADS = 4
# On the code before the change, Python 3.12 and 3.14 got between one answer in
# ten and one in four wrong at these counts (the measurements are in
# .claude/rules/gleapp-cross-platform.md), so a run does not pass by luck.
ROUNDS = {"db": 400, "http": 150}
# The long query runs once per read at most (see ``read_done`` in
# sharedreads.py), so the timeout is only reached if threads are left waiting
# on each other.
TIMEOUT = 300


def _run(tmp_path, mode: str, rounds: int) -> dict:
    case = open_case(tmp_path / "case", create=True, examiner="t")
    sharedreads.build(case.db, ROWS)
    case.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        done = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "sharedreads.py"),
             str(tmp_path / "case"), mode, str(THREADS), str(rounds)],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"the threads were still running after {TIMEOUT} s; "
                    f"output so far: {exc.stdout!r}")
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")]
    assert done.returncode == 0 and lines, (
        f"exit {done.returncode}\n{done.stdout}\n{done.stderr}")
    return json.loads(lines[-1][len("RESULT "):])


@pytest.mark.parametrize("mode", ["db", "http"])
def test_every_thread_gets_the_row_it_asked_for(tmp_path, mode):
    rounds = ROUNDS[mode]
    got = _run(tmp_path, mode, rounds)
    # the long query really did run beside the readers, and every reader finished
    assert got["slow"] > 0
    assert got["asked"] == THREADS * rounds
    assert got["ok"] + got["wrong"] + got["none"] + got["error"] == got["asked"]
    assert (got["wrong"], got["none"], got["error"]) == (0, 0, 0), got


def test_the_statement_cache_is_off_except_on_python_3_10(tmp_path, monkeypatch):
    # The run above cannot hold this half in place: on Python 3.10 a case
    # connection opened with cached_statements=0 failed it in about one run in
    # ten (the module's own cache then holds five statements, and every later
    # read of one statement raised KeyError), so a 3.10 job would usually stay
    # green on that mistake. The expectation is written out here, not read
    # from gleapp.db.
    opened = []
    real = dbmod.sqlite3.connect

    def connect(*args, **kwargs):
        opened.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)
    dbmod.CaseDB(tmp_path / "case.gleapp").close()
    assert len(opened) == 1 and opened[0].get("check_same_thread") is False
    if sys.version_info >= (3, 11):
        assert opened[0].get("cached_statements") == 0
    else:
        assert "cached_statements" not in opened[0]
