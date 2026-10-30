"""A matching run reads the perceptual-hash entries once and compares them together,
and finds exactly what comparing them one at a time per file found.

Before, ``match_all`` read every pHash entry (the case's sets and the global store)
again for each file with no exact hit and compared them one by one, so a run cost
files x entries: 11.8 ms a file against 10,000 entries, 43 minutes over 218,000
files."""
from __future__ import annotations

import random
import sqlite3

from gleapp import hashdb, hashstore, pipeline
from gleapp.case import open_case
from gleapp.hashing import hamming


def _old_best(entries, ph, threshold, use_vic):
    """The loop match_all ran before, kept verbatim as the reference."""
    # pylint: disable=unsubscriptable-object  # best is a tuple once set
    best = None
    for e in entries:
        if "vic" in e.keys() and e["vic"] and not use_vic:
            continue
        d = hamming(ph, e["v"])
        if d <= threshold and (best is None or d < best[0]):
            best = (d, {"name": e["n"], "kind": e["k"], "category": e["c"],
                        "via": f"phash~{d}",
                        "vic": bool("vic" in e.keys() and e["vic"])})
    return best[1] if best else None


def _global_rows(values):
    """Rows shaped like hashstore.iter_phash's: no ``vic`` column."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE g (v, c, n, k)")
    con.executemany("INSERT INTO g VALUES (?,?,?,?)",
                    [(v, 2, "global-set", "known") for v in values])
    return list(con.execute("SELECT v, c, n, k FROM g ORDER BY rowid"))


def _case_with_sets(tmp_path, rng):
    case = open_case(tmp_path / "c", create=True, examiner="t")
    base = [f"{rng.getrandbits(64):016x}" for _ in range(40)]
    odd = ["", "zz" * 8, "-123456789abcdef", "0x123456789abcde", "ABCDEF0123456789",
           "1234_5678_9abc_d", "abc", "f" * 64]
    for name, vic in (("vic-set", True), ("other-set", False)):
        hid = case.db.create_hashset(name, "test", "known", vic=vic)
        values = base + odd + [f"{int(b, 16) ^ (1 << rng.randrange(64)):016x}" for b in base]
        case.db.conn.executemany(
            "INSERT OR IGNORE INTO hashset_entries(hashset_id, algo, value, category) "
            "VALUES (?, 'phash', ?, ?)", [(hid, v, 1 if vic else 3) for v in values])
    case.db.commit()
    return case, base


def test_every_best_match_is_the_one_the_old_loop_found(tmp_path, monkeypatch):
    rng = random.Random(11)
    case, base = _case_with_sets(tmp_path, rng)
    globals_ = _global_rows([f"{int(b, 16) ^ 3:016x}" for b in base[:10]] + ["", "q" * 16])
    monkeypatch.setattr(hashstore, "iter_phash", lambda: globals_)
    entries = hashdb.PhashEntries(case.db)
    probes = (base + [f"{int(b, 16) ^ (1 << rng.randrange(64)) ^ (1 << rng.randrange(64)):016x}"
                      for b in base]
              + [f"{rng.getrandbits(64):016x}" for _ in range(40)]
              + ["", "zz" * 8, "abc", "f" * 64, "0x123456789abcde", "-123456789abcdef"])
    checked = found = 0
    for ph in probes:
        for threshold in (0, 2, 6, 12):
            for use_vic in (True, False):
                want = _old_best(entries.entries, ph, threshold, use_vic)
                assert entries.best(ph, threshold, use_vic) == want, (ph, threshold, use_vic)
                checked += 1
                found += want is not None
    assert checked == len(probes) * 8 and found > 100      # the hits were exercised too
    case.close()


def test_a_match_without_entries_handed_in_reads_them_itself(tmp_path, monkeypatch):
    rng = random.Random(5)
    case, base = _case_with_sets(tmp_path, rng)
    monkeypatch.setattr(hashstore, "iter_phash", lambda: [])
    fid = case.db.upsert_file("/x/a.jpg", kind="image", md5="a" * 32, phash=base[0])
    case.db.commit()
    hit = hashdb.match_file(case.db, case.db.get_file(fid))
    assert hit and hit["via"] == "phash~0" and hit["name"] == "vic-set"
    assert hashdb.match_file(case.db, case.db.get_file(fid), use_vic=False)["name"] == "other-set"
    case.close()


def test_a_matching_run_reads_the_entries_once(tmp_path, monkeypatch):
    rng = random.Random(7)
    case, base = _case_with_sets(tmp_path, rng)
    reads = []
    monkeypatch.setattr(hashstore, "iter_phash", lambda: reads.append(1) or [])
    for i, ph in enumerate(base[:25]):
        case.db.upsert_file(f"/x/{i}.jpg", kind="image", md5=f"{i:032x}", phash=ph)
    case.db.commit()
    hits = pipeline.rematch_hashes(case)
    assert hits == 25
    assert len(reads) == 1
    case.close()
