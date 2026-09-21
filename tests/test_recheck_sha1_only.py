"""Re-check reads a file whose only hash is a SHA-1.

``rematch_hashes`` read only rows carrying an MD5 or a SHA-256, so a row whose
only hash was a SHA-1 was never checked against a hash set, and a flag it
carried was never cleared. Processing computes all three hashes for every file
it can read, so such a row is one whose hash came from a Project VIC case
export entry with no MD5, for a file missing on disk. The two case exports
GLEAPP has been measured against carried an MD5 on every entry (see
``pipeline._process_one_at``), so none has been seen. A missing file that
carries only an MD5 was already checked; this checks a SHA-1 the same way.

Everything here is synthetic.
"""

from __future__ import annotations

import pytest

from gleapp import hashdb, hashstore, stash
from gleapp.case import open_case
from gleapp.pipeline import rematch_hashes


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    cfg = tmp_path_factory.mktemp("cfg")
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("GLEAPP_STASH_PATH", str(cfg / "stash.hstash"))
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def test_a_row_with_only_a_sha1_is_matched_and_a_stale_flag_cleared(tmp_path):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        listed = tmp_path / "list.txt"
        listed.write_text("7" * 40 + "\n", encoding="utf-8")
        hashdb.import_hashset(case.db, listed, name="SHA-1 list", kind="known")
        missing = case.db.upsert_file("/x/missing.jpg", kind="image", sha1="7" * 40,
                                      error="file not found on disk")
        stale = case.db.upsert_file("/x/stale.jpg", kind="image", sha1="9" * 40,
                                    hashset_hit="A removed set", hashset_cat=2,
                                    hashset_kind="known")
        case.db.commit()

        assert rematch_hashes(case) == 1
        assert case.db.get_file(missing)["hashset_hit"] == "SHA-1 list"
        row = case.db.get_file(stale)
        assert row["hashset_hit"] is None and row["hashset_cat"] is None
    finally:
        case.close()
