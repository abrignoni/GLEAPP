"""Agency logo: an app-wide default, set from ☰ Settings on the launcher, that
goes on every case's report header unless that case sets its own from its own
Export dialog (the pre-existing per-case field).
"""

import base64
import io
from pathlib import Path

from PIL import Image

TEMPLATE = Path(__file__).resolve().parents[1] / "gleapp/web/templates/index.html"


def _logo_uri(color=(1, 2, 3)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_launcher_has_a_settings_entry_and_dialog_for_the_logo():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="btnLogoLauncher"' in html
    assert 'id="logoDlg"' in html
    assert 'id="logoFile"' in html


def test_the_new_case_form_has_no_logo_field_of_its_own():
    """The logo lives only in ☰ Settings - the New Case form must not grow a
    second place to set it."""
    html = TEMPLATE.read_text(encoding="utf-8")
    card = html[html.index("<h2>New case</h2>"):]
    card = card[:card.index("Evidence to ingest")]
    assert "newLogoFile" not in card
    assert "logo" not in card.lower()


def test_setting_the_default_logo_round_trips(tmp_path):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel

    cl = create_app(None).test_client()
    assert cl.get("/api/context").get_json()["agency_logo"] is None

    uri = _logo_uri()
    r = cl.post("/api/settings", json={"agency_logo": uri})
    assert r.status_code == 200 and r.get_json()["agency_logo"] == uri
    assert cl.get("/api/context").get_json()["agency_logo"] == uri

    r = cl.post("/api/settings", json={"agency_logo": None})
    assert r.status_code == 200 and r.get_json()["agency_logo"] is None
    assert cl.get("/api/context").get_json()["agency_logo"] is None


def test_setting_the_default_logo_rejects_a_non_image_value():
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel

    cl = create_app(None).test_client()
    r = cl.post("/api/settings", json={"agency_logo": "not a data uri"})
    assert r.status_code == 400


def test_case_create_ignores_a_logo_field_if_one_is_sent(tmp_path):
    """/api/case/create takes no logo of its own - only report_header, set
    from inside the case's Export dialog, can give a case its own logo."""
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel

    cl = create_app(None).test_client()
    r = cl.post("/api/case/create",
                json={"path": str(tmp_path / "c1"), "name": "C1", "logo": _logo_uri()})
    assert r.status_code == 200
    prefs = cl.get("/api/report/prefs").get_json()
    assert not prefs["header"].get("logo")


def test_a_case_with_no_logo_of_its_own_falls_back_to_the_default(tmp_path):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel

    cl = create_app(None).test_client()
    uri = _logo_uri()
    cl.post("/api/settings", json={"agency_logo": uri})

    r = cl.post("/api/case/create", json={"path": str(tmp_path / "c1"), "name": "C1"})
    assert r.status_code == 200
    prefs = cl.get("/api/report/prefs").get_json()
    assert prefs["header"]["logo"] == uri


def test_exporting_the_fallback_logo_adopts_it_as_the_cases_own(tmp_path):
    """Once an examiner exports with the pre-filled default still showing, that
    case should keep using it even if the app-wide default later changes -
    same as agency/case number/examiner/notes already behave."""
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel

    cl = create_app(None).test_client()
    default_uri = _logo_uri((7, 8, 9))
    cl.post("/api/settings", json={"agency_logo": default_uri})
    r = cl.post("/api/case/create", json={"path": str(tmp_path / "c5"), "name": "C5"})
    assert r.status_code == 200

    prefs = cl.get("/api/report/prefs").get_json()
    assert prefs["header"]["logo"] == default_uri
    # simulate the Export dialog submitting the pre-filled header as-is
    r = cl.post("/api/report", json={
        "format": ["csv"], "scope": "all", "report_header": prefs["header"]})
    assert r.status_code == 200

    # the app-wide default changes...
    cl.post("/api/settings", json={"agency_logo": _logo_uri((1, 1, 1))})
    # ...but this case kept the one it exported with
    prefs2 = cl.get("/api/report/prefs").get_json()
    assert prefs2["header"]["logo"] == default_uri
