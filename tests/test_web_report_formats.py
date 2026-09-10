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


def _dialog_formats() -> set[str]:
    """The format values the export dialog's checkboxes carry."""
    html = TEMPLATE.read_text(encoding="utf-8")
    dlg = html[html.index('<div id="reportDlg">'):]
    dlg = dlg[:dlg.index("</div></div>")]
    return set(re.findall(r'class="rfmt"\s+value="([a-z0-9]+)"', dlg))


def test_the_dialog_offers_every_format_the_route_writes():
    from gleapp.web.app import REPORT_FORMATS          # pylint: disable=import-outside-toplevel
    assert _dialog_formats() == EXPECTED_FORMATS
    assert set(REPORT_FORMATS) == EXPECTED_FORMATS


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
    """Only LAVA is slow enough to need a job; the rest stay inline."""
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    path = _case_with_one_image(tmp_path)
    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(path)})
    body = cl.post("/api/report", json={"format": ["csv"], "scope": "all"}).get_json()
    assert "job" not in body
    assert len(body["written"]) == 1 and body["written"][0].endswith(".csv")
