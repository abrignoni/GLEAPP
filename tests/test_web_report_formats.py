"""The export dialog has to offer every format the export route can write.

The LAVA report was the case that made this worth a test: the route grew it
before the dialog did, so the one output that carries the media and the
location maps into the viewer the LEAPP family already uses could be produced
from the command line and not from the gallery an examiner works in.
"""

import re
import time
from pathlib import Path

from PIL import Image

# Written out as literals, not read from the code, so this fails when either
# side changes alone.
EXPECTED_FORMATS = {"html", "csv", "json", "kml", "md5", "vic", "lava"}

TEMPLATE = Path(__file__).resolve().parents[1] / "gleapp/web/templates/index.html"
APP_JS = Path(__file__).resolve().parents[1] / "gleapp/web/static/app.js"


def _dialog_formats() -> set[str]:
    """The format values the export dialog's checkboxes carry."""
    html = TEMPLATE.read_text(encoding="utf-8")
    dlg = html[html.index('<div id="reportDlg">'):]
    dlg = dlg[:dlg.index("</div></div>")]
    return set(re.findall(r'class="rfmt"\s+value="([a-z0-9]+)"', dlg))


def _dialog() -> str:
    """The export dialog's own markup."""
    html = TEMPLATE.read_text(encoding="utf-8")
    dlg = html[html.index('<div id="reportDlg">'):]
    return dlg[:dlg.index("</div></div>")]


def test_the_dialog_offers_every_format_the_route_writes():
    from gleapp.web.app import REPORT_FORMATS          # pylint: disable=import-outside-toplevel
    assert _dialog_formats() == EXPECTED_FORMATS
    assert set(REPORT_FORMATS) == EXPECTED_FORMATS


def test_only_html_is_checked_by_default():
    """CSV used to be pre-checked alongside HTML; an examiner who only wanted
    the report and clicked Export got a second file they did not ask for."""
    html = TEMPLATE.read_text(encoding="utf-8")
    dlg = html[html.index('<div id="reportDlg">'):]
    dlg = dlg[:dlg.index("</div></div>")]
    checked = set(re.findall(r'class="rfmt"\s+value="([a-z0-9]+)"\s+checked', dlg))
    assert checked == {"html"}


def _wait_for_export(cl):
    """An HTML export runs as a job so the bottom bar can follow it; wait for it."""
    for _ in range(240):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            assert job["stage"] == "done", job
            return job
        time.sleep(0.25)
    raise AssertionError(f"export still running: {job}")


def _case_with_one_image(tmp_path):
    from gleapp.case import Source, open_case          # pylint: disable=import-outside-toplevel
    from gleapp.pipeline import ingest_sources, process  # pylint: disable=import-outside-toplevel
    ev = tmp_path / "ev"
    ev.mkdir()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(ev / "a.jpg")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    process(case, workers=1, keyframes=0, screen=False)
    case.close()
    return tmp_path / "case"


def test_asking_for_lava_from_the_gallery_writes_a_lava_project(tmp_path):
    """The whole point of the finding: it has to be reachable from the UI."""
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    path = _case_with_one_image(tmp_path)
    cl = create_app(None).test_client()
    assert cl.post("/api/case/open", json={"path": str(path)}).status_code == 200

    r = cl.post("/api/report", json={"format": ["lava"], "scope": "all"})
    assert r.status_code == 200
    body = r.get_json()
    # It stages media and draws maps, so it runs as a job rather than blocking
    # the request until it is done.
    assert body["job"] is True

    for _ in range(120):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            break
        time.sleep(0.25)
    assert job["stage"] == "done", job
    written = job["stats"]["written"]
    assert len(written) == 1 and Path(written[0]).name == "_lava_data.lava"
    assert Path(written[0]).is_file()
    assert (Path(written[0]).parent / "_lava_artifacts.db").is_file()


def test_the_other_formats_still_come_back_in_the_response(tmp_path):
    """LAVA and HTML are slow enough to need a job; the rest stay inline."""
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    path = _case_with_one_image(tmp_path)
    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(path)})
    body = cl.post("/api/report", json={"format": ["csv"], "scope": "all"}).get_json()
    assert "job" not in body
    assert len(body["written"]) == 1 and body["written"][0].endswith(".csv")


def test_an_html_report_runs_as_a_job_the_bottom_bar_can_follow(tmp_path):
    """An HTML report embeds every file's full-size view, which is minutes on a real
    case; inline, the gallery showed nothing until it was done."""
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    path = _case_with_one_image(tmp_path)
    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(path)})
    body = cl.post("/api/report", json={"format": ["html"], "scope": "all"}).get_json()
    assert body["job"] is True
    job = _wait_for_export(cl)
    assert job["total"] == 1 and job["done"] == 1
    assert [Path(p).name for p in job["stats"]["written"]] == ["report.html"]


def test_the_dialog_offers_the_maps_toggle_the_route_reads():
    """The same drift as the LAVA format, in an option rather than a format.

    ``/api/report`` read ``maps`` from the request body from the day the report
    drew maps, and the dialog never sent it, so the maps could only be left out
    from the command line while the README and the manual said otherwise.
    """
    dlg = _dialog()
    assert 'id="rhMaps"' in dlg, "the export dialog has no maps control"
    js = APP_JS.read_text(encoding="utf-8")
    assert 'body.maps = $("#rhMaps").checked;' in js, "the dialog does not send maps"
    assert '$("#rhMaps").checked = p.maps !== false;' in js, "the choice is not restored"


def test_unticking_maps_reaches_the_writer_and_is_remembered(tmp_path, monkeypatch):
    """Sending ``maps: false`` has to arrive at the report writer, not just be
    accepted, and come back to the dialog the way the Media checkboxes do."""
    from gleapp import report                          # pylint: disable=import-outside-toplevel
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    path = _case_with_one_image(tmp_path)
    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(path)})

    seen = {}
    real = report.export_html

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(report, "export_html", spy)
    assert cl.post("/api/report",
                   json={"format": ["html"], "scope": "all",
                         "maps": False}).status_code == 200
    _wait_for_export(cl)
    assert seen.get("maps") is False, seen
    assert cl.get("/api/report/prefs").get_json()["maps"] is False

    seen.clear()
    cl.post("/api/report", json={"format": ["html"], "scope": "all", "maps": True})
    _wait_for_export(cl)
    assert seen.get("maps") is True, seen
    assert cl.get("/api/report/prefs").get_json()["maps"] is True


def test_maps_are_drawn_when_the_request_says_nothing(tmp_path, monkeypatch):
    """A request with no ``maps`` key still gets maps, so the CLI default and the
    dialog default cannot drift apart."""
    from gleapp import report                          # pylint: disable=import-outside-toplevel
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    path = _case_with_one_image(tmp_path)
    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(path)})

    seen = {}
    real = report.export_html
    monkeypatch.setattr(report, "export_html",
                        lambda *a, **kw: (seen.update(kw), real(*a, **kw))[1])
    cl.post("/api/report", json={"format": ["html"], "scope": "all"})
    _wait_for_export(cl)
    assert seen.get("maps") is True, seen
