"""The hash stash panel's eligible count is the number Add submits.

The panel counted every category 1-3 file with an MD5. Add keeps an MD5 only
when it is 32 hex characters and not the MD5 of an empty file, so a case whose
only category 1-3 file was empty offered "Add this case's 1 hash(es)" and added
none. The count now goes through the same check as Add.

Everything here is synthetic.
"""

from __future__ import annotations

import pytest

from gleapp import hashstore, stash
from gleapp.case import open_case

_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    cfg = tmp_path_factory.mktemp("cfg")
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(cfg))
    # the stash path is read first from this, so each test starts with an empty one
    monkeypatch.setenv("GLEAPP_STASH_PATH", str(cfg / "stash.hstash"))
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def test_the_eligible_count_is_what_add_submits(tmp_path):
    from gleapp.web.app import create_app
    case = open_case(tmp_path / "case", create=True, examiner="t")
    for path, md5, category in (
        ("/x/kept.jpg", "a" * 32, 1),
        ("/x/upper.jpg", "B" * 32, 3),               # kept, in lower case
        ("/x/empty.jpg", _EMPTY_MD5, 1),             # the MD5 of an empty file
        ("/x/empty_upper.jpg", _EMPTY_MD5.upper(), 2),
        ("/x/short.jpg", "c" * 31, 2),               # not an MD5
        ("/x/uncategorized.jpg", "d" * 32, 0),       # not in category 1-3
    ):
        case.db.upsert_file(path, kind="image", md5=md5, category=category)
    case.db.commit()
    case.close()

    cl = create_app(str(tmp_path / "case")).test_client()
    status = cl.get("/api/stash").get_json()["case"]
    assert status["eligible"] == 2
    assert status["by_category"] == {"1": 1, "3": 1}
    res = cl.post("/api/stash/add").get_json()
    assert res["submitted"] == status["eligible"]
    assert res["added"] == 2
    assert sorted(md5 for md5, *_ in stash.iter_all()) == ["a" * 32, "b" * 32]
