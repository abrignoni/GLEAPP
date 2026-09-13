"""Open existing case: a folder browse and a case.gleapp file browse.

The launcher only offered a folder picker. An examiner who has a case.gleapp
path copied (from a report footer, a colleague's message, a shortcut) had to
strip the filename back off to type a folder instead. Added a second browse
button restricted to *.gleapp, and /api/case/open now accepts either the
case folder or a direct path to the case.gleapp file inside it.
"""

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "gleapp/web/templates/index.html"


def _empty_case(tmp_path):
    from gleapp.case import open_case  # pylint: disable=import-outside-toplevel
    case = open_case(tmp_path / "case", create=True, examiner="t")
    case.close()
    return tmp_path / "case"


def test_case_open_accepts_the_folder(tmp_path):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    path = _empty_case(tmp_path)
    cl = create_app(None).test_client()
    assert cl.post("/api/case/open", json={"path": str(path)}).status_code == 200
    assert cl.get("/api/context").get_json()["needs_case"] is False


def test_case_open_also_accepts_the_case_gleapp_file_directly(tmp_path):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    path = _empty_case(tmp_path)
    cl = create_app(None).test_client()
    r = cl.post("/api/case/open", json={"path": str(path / "case.gleapp")})
    assert r.status_code == 200
    assert cl.get("/api/context").get_json()["needs_case"] is False


def test_case_open_still_404s_on_a_folder_with_no_case(tmp_path):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    (tmp_path / "empty").mkdir()
    cl = create_app(None).test_client()
    r = cl.post("/api/case/open", json={"path": str(tmp_path / "empty")})
    assert r.status_code == 404


def test_pick_casefile_dialog_is_restricted_to_gleapp(monkeypatch):
    """Desktop mode only; a fake webview module stands in for pywebview so the
    test doesn't need a real window."""
    from gleapp.web import app as appmod  # pylint: disable=import-outside-toplevel

    seen = {}

    class FakeWin:
        def create_file_dialog(self, kind, file_types=()):
            seen["kind"] = kind
            seen["file_types"] = file_types
            return ["C:/cases/op1/case.gleapp"]

    class FakeWebview:
        OPEN_DIALOG = "open"
        FOLDER_DIALOG = "folder"
        windows = [FakeWin()]

    monkeypatch.setitem(__import__("sys").modules, "webview", FakeWebview)

    cl = appmod.create_app(None, native=True).test_client()
    resp = cl.post("/api/pick", json={"kind": "casefile"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["path"] == "C:/cases/op1/case.gleapp"
    assert seen["kind"] == "open"
    assert any("*.gleapp" in ft for ft in seen["file_types"])


def test_launcher_has_a_folder_and_a_file_browse_button():
    html = TEMPLATE.read_text(encoding="utf-8")
    card = html[html.index("<h2>Open existing case</h2>"):]
    card = card[:card.index("</div>\n  </div>") + len("</div>\n  </div>")]
    assert 'id="openBrowse"' in card
    assert 'id="openBrowseFile"' in card
