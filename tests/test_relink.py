"""Relinking a folder source (a folder ingest or a Project VIC import) that moved."""

import shutil

import pytest
from PIL import Image

from gleapp import relink
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))


def _folder_case(tmp_path):
    ev = tmp_path / "ev"
    (ev / "sub").mkdir(parents=True)
    for i, where in enumerate(("a.png", "b.png", "sub/c.png")):
        Image.new("RGB", (32, 32), (i * 60, 90, 200 - i * 50)).save(ev / where)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(name="ev", path=str(ev))])
    process(case, workers=1, keyframes=2, screen=False)
    return case, ev


def _paths(case):
    return sorted(r["path"] for r in case.db.conn.execute("SELECT path FROM files"))


def test_a_moved_folder_is_reported_and_relinked(tmp_path):
    case, ev = _folder_case(tmp_path)
    try:
        assert relink.folder_status(case) == [
            {"name": "ev", "root": str(ev), "files": 3, "status": "ok"}]
        moved = tmp_path / "moved"
        shutil.move(str(ev), str(moved))
        assert relink.folder_status(case)[0]["status"] == "missing"

        r = relink.relink_folder(case, "ev", moved)
        assert r["files"] == 3
        assert _paths(case) == sorted(str(moved / p) for p in ("a.png", "b.png", "sub/c.png"))
        assert relink.folder_status(case)[0]["status"] == "ok"
        audit = [a["action"] for a in case.db.conn.execute("SELECT action FROM audit")]
        assert "relink-folder" in audit
    finally:
        case.close()


def test_a_folder_missing_a_file_or_holding_a_different_one_is_refused(tmp_path):
    case, ev = _folder_case(tmp_path)
    try:
        before = _paths(case)
        moved = tmp_path / "moved"
        shutil.copytree(ev, moved)
        (moved / "sub" / "c.png").unlink()
        with pytest.raises(ValueError, match="missing"):
            relink.relink_folder(case, "ev", moved)
        assert _paths(case) == before

        # same size, different bytes: only the MD5 can tell
        data = bytearray((ev / "sub" / "c.png").read_bytes())
        data[-5] ^= 0xFF
        (moved / "sub" / "c.png").write_bytes(bytes(data))
        with pytest.raises(ValueError, match="different MD5"):
            relink.relink_folder(case, "ev", moved)
        assert _paths(case) == before

        with pytest.raises(ValueError, match="not a folder"):
            relink.relink_folder(case, "ev", tmp_path / "nowhere")
    finally:
        case.close()


def test_relink_folder_endpoint_runs_as_a_job(tmp_path):
    import time

    from gleapp.web.app import create_app

    case, ev = _folder_case(tmp_path)
    root = case.root
    case.close()
    moved = tmp_path / "moved"
    shutil.move(str(ev), str(moved))

    client = create_app(str(root)).test_client()
    ctx = client.get("/api/context").get_json()
    assert ctx["folder_sources"][0]["status"] == "missing"
    assert client.get("/api/source/folder-status").get_json()[0]["status"] == "missing"
    assert client.post("/api/source/relink-folder",
                       json={"name": "ev", "path": str(tmp_path / "nowhere")}).status_code == 400
    assert client.post("/api/source/relink-folder",
                       json={"name": "ev", "path": str(moved)}).status_code == 200
    for _ in range(100):
        job = client.get("/api/job").get_json()
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["stage"] == "done", job
    assert client.get("/api/context").get_json()["folder_sources"][0]["status"] == "ok"
