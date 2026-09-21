"""The "Expand archives" option on the ingest screens.

Opening every archive found inside a source is slow on a full file-system extraction that
holds many thousands of compressed files, so the launcher and the Add evidence dialog carry
a checkbox for it. Off, the archives are still registered as containers (and can be opened
later with the sidebar's Expand archives); on, or when a caller sends no option at all, the
media inside them is registered in the same pass, as before.
"""

import io
import re
import time
import zipfile
from pathlib import Path

from PIL import Image

from gleapp.case import open_case


def _jpg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, "JPEG")
    return buf.getvalue()


def _client():
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    return create_app(None).test_client()


def _wait(cl, timeout=60):
    for _ in range(timeout * 4):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            return job
        time.sleep(0.25)
    raise AssertionError("job never finished")


def _evidence(tmp_path) -> Path:
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "loose.jpg").write_bytes(_jpg((200, 40, 40)))
    with zipfile.ZipFile(ev / "Donkeys.zip", "w") as zf:
        zf.writestr("donkey1.jpg", _jpg((150, 90, 60)))
        zf.writestr("donkey2.jpg", _jpg((90, 60, 40)))
    return ev


def _ingest(tmp_path, options):
    cl = _client()
    assert cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "c",
                                             "examiner": "t"}).status_code == 200
    r = cl.post("/api/case/ingest", json={"sources": [{"path": str(_evidence(tmp_path))}],
                                          "options": options})
    assert r.status_code == 200, r.get_json()
    job = _wait(cl)
    assert job["stage"] == "done", job
    case = open_case(tmp_path / "c")
    try:
        return {r["rel_path"]: dict(r) for r in case.db.iter_files()}
    finally:
        case.close()


_BASE = {"screen": False, "keyframes": 0}


def test_unticked_registers_the_archive_but_does_not_open_it(tmp_path):
    rows = _ingest(tmp_path, {**_BASE, "expand_archives": False})
    assert rows["Donkeys.zip"]["kind"] == "archive", "the container is still registered"
    assert not [p for p in rows if p.startswith("Donkeys.zip/")], "nothing was opened"
    assert "loose.jpg" in rows


def test_ticked_opens_the_archive(tmp_path):
    rows = _ingest(tmp_path, {**_BASE, "expand_archives": True})
    cid = rows["Donkeys.zip"]["id"]
    kids = [r for r in rows.values() if r["container_id"] == cid]
    assert len(kids) == 2 and {r["kind"] for r in kids} == {"image"}


def test_a_caller_that_sends_no_option_still_gets_the_old_behaviour(tmp_path):
    """The API, the CLI and older clients say nothing about it: expansion stays on."""
    rows = _ingest(tmp_path, dict(_BASE))
    assert any(p.startswith("Donkeys.zip/") for p in rows)


def test_the_archives_can_still_be_opened_afterwards(tmp_path):
    """Unticked is not lost: the sidebar's Expand archives runs the same step later."""
    rows = _ingest(tmp_path, {**_BASE, "expand_archives": False})
    assert "Donkeys.zip" in rows
    cl = _client()
    assert cl.post("/api/case/open", json={"path": str(tmp_path / "c")}).status_code == 200
    assert cl.post("/api/expand-archives", json={}).status_code == 200
    assert _wait(cl)["stage"] == "done"
    case = open_case(tmp_path / "c")
    try:
        assert any(r["rel_path"].startswith("Donkeys.zip/") for r in case.db.iter_files())
    finally:
        case.close()


def test_both_ingest_screens_carry_the_checkbox_and_send_it():
    root = Path(__file__).resolve().parent.parent / "gleapp" / "web"
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    for box in ("optExpand", "aeExpand"):
        assert f'id="{box}"' in html, f"{box} is missing from the page"
        # unticked to start with: the user ticks it when a source needs it
        assert re.search(rf'id="{box}"(?! checked)', html)
        assert f'$("#{box}").checked' in js, f"{box} is never read"
    assert js.count("expand_archives:") == 2, "both ingest requests must send the option"
