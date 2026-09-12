"""Report + export generation: CSV, JSON, HTML, and KML for geolocation."""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import time
import zipfile
from collections import Counter
from pathlib import Path

from . import basemaps, categories, imaging, staticmap, timeutil  # noqa: F401  (imaging: registers HEIF decoder)
from .case import Case

_CSV_FIELDS = [
    "id", "file_path", "disk_name", "source", "kind", "ext", "size",
    "created_dt", "ctime", "mtime", "atime",
    "md5", "sha1", "sha256", "phash", "width", "height", "duration",
    "gps_lat", "gps_lon", "camera", "faces", "skin_ratio",
    "category", "category_label", "triage",
    "hashset_hit", "hashset_cat", "hashset_kind",
    "stack_id", "vstack_id", "cluster_id", "notes",
    "media_id", "orig_name", "orig_path", "mime", "origin",
]


def export_projectvic(case: Case, dest: str | Path, *,
                      only_categorized: bool = False) -> Path:
    """Round-trip export to Project VIC 2.0 JSON (see gleapp.projectvic)."""
    from . import projectvic
    return projectvic.export_vic(case, dest, only_categorized=only_categorized)


def _rows(case: Case, where: str = "") -> list[dict]:
    # An archive container (.zip / .tar found in a source) is not media - its
    # members are their own rows. Keep it out of every report unless the scope
    # asked for it by name.
    if "kind" not in where:
        where = _and(where, "kind != 'archive'")
    out = []
    for r in case.db.iter_files(where):
        d = dict(r)
        d["category_label"] = categories.label(case.db, d.get("category") or 0)
        d["tags"] = case.db.tags_for(d["id"])
        d["file_path"] = _disp_path(d)      # device path (VIC) or source path
        d["disk_name"] = _disk_name(d)      # the on-disk (MD5) name
        out.append(d)
    return out


def _and(*clauses: str) -> str:
    return " AND ".join(f"({c})" for c in clauses if c)


def export_md5(case: Case, dest: str | Path, where: str = "") -> Path:
    """CSV of the distinct MD5 values in scope: header ``md5``, one per line."""
    dest = Path(dest)
    sql = "SELECT DISTINCT md5 FROM files WHERE md5 IS NOT NULL AND md5 != ''"
    if "kind" not in where:
        sql += " AND kind != 'archive'"
    if where:
        sql += f" AND ({where})"
    sql += " ORDER BY md5"
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["md5"])
        for (h,) in case.db.conn.execute(sql):
            w.writerow([h])
    return dest


def export_csv(case: Case, dest: str | Path, where: str = "", *,
               tz: str | None = None) -> Path:
    global _TZ
    _TZ = tz
    dest = Path(dest)
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in _rows(case, where):
            for k in ("ctime", "mtime", "atime"):     # epoch -> readable
                if d.get(k):
                    d[k] = _fmt_ts(d[k])
            w.writerow(d)
    return dest


def export_json(case: Case, dest: str | Path, where: str = "",
                *, header: dict | None = None) -> Path:
    dest = Path(dest)
    payload = {
        "case": case.db.get_meta("case_name"),
        "examiner": case.examiner,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "report_header": {k: v for k, v in (header or {}).items() if k != "logo"},
        "stats": case.db.stats(),
        "basemap": ({"name": case.db.get_meta("basemap_name"),
                     "format": case.db.get_meta("basemap_format"),
                     "sha256": case.db.get_meta("basemap_sha256")}
                    if case.db.get_meta("basemap_sha256") else None),
        # files.path is where the bytes sat on this machine; rel_path, orig_path and
        # file_path say where the file was within the evidence, and that is what leaves.
        "files": [{k: v for k, v in d.items() if k != "path"} for d in _rows(case, where)],
    }
    dest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return dest


def export_kml(case: Case, dest: str | Path, where: str = "") -> Path:
    """KMZ of every in-scope file that carries GPS coordinates.

    The archive bundles a thumbnail (or, for a video, its middle key frame)
    for each placemark, so clicking a pin in Google Earth shows the picture
    right at its location.
    """
    dest = Path(dest).with_suffix(".kmz")
    pts = _rows(case, _and("gps_lat IS NOT NULL AND gps_lon IS NOT NULL", where))

    media: dict[str, bytes] = {}          # arcname -> file bytes
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        f"<name>{html.escape(str(case.db.get_meta('case_name')))} - media geolocation</name>",
    ]
    for d in pts:
        img_html = ""
        thumb = _thumb_bytes(case, d)
        if thumb:
            arc = f"files/{d['id']}.jpg"
            media[arc] = thumb
            img_html = f'<img src="{arc}" width="320"/><br/>'
        desc = (
            f"{img_html}"
            f"{html.escape(_disp_path(d))}<br/>"
            f"{html.escape(str(d.get('created_dt') or ''))}<br/>"
            f"Category: {html.escape(d['category_label'])}"
        )
        parts.append(
            "<Placemark>"
            f"<name>{html.escape(_disp_name(d))}</name>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{d['gps_lon']},{d['gps_lat']},0</coordinates></Point>"
            "</Placemark>"
        )
    parts.append("</Document></kml>")

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", "\n".join(parts))
        for arc, raw in media.items():
            z.writestr(arc, raw)
    return dest


def _thumb_bytes(case: Case, d: dict) -> bytes | None:
    """Raw JPEG bytes of the best small preview for a file, or None."""
    names: list[str] = []
    if d.get("kind") == "video":
        kfs = case.db.conn.execute(
            "SELECT thumb FROM keyframes WHERE file_id=? AND thumb IS NOT NULL "
            "ORDER BY ts", (d["id"],)).fetchall()
        if kfs:
            names.append(kfs[len(kfs) // 2]["thumb"])
    if d.get("thumb"):
        names.append(d["thumb"])
    for name in names:
        try:
            return (case.thumb_dir / name).read_bytes()
        except OSError:
            continue
    return None


# display timezone for the current export (set by export_html / export_csv)
_TZ: str | None = None


def _fmt_ts(v) -> str:
    """Epoch seconds -> readable string in the report's display timezone."""
    return timeutil.fmt_epoch(v, _TZ, fmt="%Y-%m-%d %H:%M %Z")


def _fmt_size(b) -> str:
    if not b:
        return ""
    b = float(b)
    return f"{b / 1048576:.1f} MB" if b > 1e6 else f"{b / 1024:.0f} KB"


def _fmt_dur(s) -> str:
    if not s:
        return ""
    s = int(float(s))
    return f"{s // 60}:{s % 60:02d}"


def _disk_name(d: dict) -> str:
    """The name the file is stored under on disk (an MD5 for VIC imports)."""
    return Path(d.get("rel_path") or d.get("path") or "").name


def _disp_name(d: dict) -> str:
    """Preferred display name: the original file name, else the stored name."""
    return d.get("orig_name") or _disk_name(d) or f"file #{d.get('id')}"


def _disp_path(d: dict) -> str:
    """Where the file lived on the device (Project VIC MediaFiles.FilePath, an
    archive member's path), else - for a plain folder ingest - the path within that
    source. Never a location on the machine the case was made on: not the folder a
    VIC export was unpacked into, not the evidence folder, not the case folder."""
    if d.get("orig_path"):
        return d["orig_path"]
    if d.get("media_id"):          # a VIC file with no MediaFiles path
        return ""
    return d.get("rel_path") or Path(d.get("path") or "").name


def _recorded(d) -> str:
    """The times the filesystem stored for a file, exactly as stored.

    FAT and exFAT keep a wall-clock reading and no zone at all, so these are
    readings and not instants, and the FS written / created / accessed columns
    beside them are empty for such a volume. They are rendered as plain text
    deliberately: a datetime column would be given a zone by whatever displays
    it, and that zone would be invented.

    exFAT also stores a UTC offset per timestamp. It is shown as stored rather
    than applied, because on the one exFAT volume measured the stored reading
    was not the writing machine's local clock and the offset was the negation of
    its zone, so resolving the pair would assert an instant on evidence that
    cannot support it.
    """
    raw = d.get("recorded_times")
    if not raw:
        return ""
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(got, dict):
        return ""
    return "; ".join(f"{k} {v}" for k, v in got.items() if v)


# files.origin (db.ORIGINS) as a phrase, for the surfaces a person reads. The
# stored value goes out unchanged in the CSV and JSON exports, which are read by
# a machine; the HTML report and the LAVA artifact show these instead, and both
# take them from here so the wording cannot drift between them.
_ORIGIN_LABELS = {
    "walk": "walked, still listed",
    "deleted": "recovered from a deleted record",
    "carve": "carved from unclaimed space",
}


def _origin_label(d) -> str:
    """How this row's file was recovered, in words.

    Empty for a row with no origin recorded, which is every row of a case built
    before the column existed and every row from a folder or archive source: the
    question only has an answer for an acquisition. An unrecognised value is
    passed through as stored rather than dropped, so a value this version does
    not know about is still visible to whoever reads the report.
    """
    got = d.get("origin") or ""
    return _ORIGIN_LABELS.get(got, got)


# key -> (label, value fn, is_monospace).  The report dialog offers exactly
# these; the examiner picks which appear under each image.
_FIELD_DEFS: dict[str, tuple[str, "callable", bool]] = {
    "name":       ("File name",     lambda d: _disp_name(d), False),
    "disk_name":  ("Stored name",   lambda d: _disk_name(d), True),
    "path":       ("Path",          _disp_path, False),
    "orig_name":  ("Original name", lambda d: d.get("orig_name") or "", False),
    "orig_path":  ("Device path",   lambda d: d.get("orig_path") or "", False),
    "created_dt": ("Captured (EXIF)", lambda d: str(d.get("created_dt") or ""), False),
    "ctime":      ("FS created",    lambda d: _fmt_ts(d.get("ctime")), False),
    "mtime":      ("FS written",    lambda d: _fmt_ts(d.get("mtime")), False),
    "atime":      ("FS accessed",   lambda d: _fmt_ts(d.get("atime")), False),
    "recorded_times": ("Recorded (as stored, no zone)", _recorded, False),
    "ingested_at": ("Ingested",     lambda d: _fmt_ts(d.get("ingested_at")), False),
    "md5":        ("MD5",           lambda d: d.get("md5") or "", True),
    "sha1":       ("SHA-1",         lambda d: d.get("sha1") or "", True),
    "sha256":     ("SHA-256",       lambda d: d.get("sha256") or "", True),
    "phash":      ("pHash",         lambda d: d.get("phash") or "", True),
    "dimensions": ("Dimensions",    lambda d: f"{d['width']}x{d['height']}" if d.get("width") else "", False),
    "size":       ("File size",     lambda d: _fmt_size(d.get("size")), False),
    "duration":   ("Duration",      lambda d: _fmt_dur(d.get("duration")), False),
    "camera":     ("Camera",        lambda d: d.get("camera") or "", False),
    "gps":        ("GPS",           lambda d: (f"{d['gps_lat']:.6f}, {d['gps_lon']:.6f}"
                                               if d.get("gps_lat") is not None else ""), False),
    "category":   ("Category",      lambda d: d.get("category_label") or "", False),
    "tags":       ("Tags",          lambda d: ", ".join(d.get("tags") or []), False),
    "notes":      ("Notes",         lambda d: d.get("notes") or "", False),
    "faces":      ("Faces",         lambda d: str(d["faces"]) if d.get("faces") else "", False),
    "skin_ratio": ("Skin ratio",    lambda d: f"{d['skin_ratio']:.2f}" if d.get("skin_ratio") else "", False),
    "source":     ("Source",        lambda d: d.get("source") or "", False),
    "origin":     ("How recovered", _origin_label, False),
    "mime":       ("MIME type",     lambda d: d.get("mime") or "", False),
    "media_id":   ("VIC MediaID",   lambda d: str(d["media_id"]) if d.get("media_id") is not None else "", False),
    "hashset":    ("Known hash",    lambda d: d.get("hashset_hit") or "", False),
    "error":      ("Error",         lambda d: d.get("error") or "", False),
}
DEFAULT_REPORT_FIELDS = ["name", "created_dt", "md5"]

_HTML_HEAD = """<!doctype html><html class="blur"><head><meta charset="utf-8">
<title>{case} - report</title>
<style>
 :root{{--line:#d5d9e0;--ink:#1a1d24;--mut:#5b6472}}
 *{{box-sizing:border-box}}
 body{{font:13px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;
   color:var(--ink);background:#fff}}
 .wrap{{max-width:1200px;margin:0 auto;padding:24px}}
 .muted{{color:var(--mut)}}
 header.rpt{{display:flex;gap:20px;align-items:flex-start;border-bottom:2px solid var(--ink);
   padding-bottom:14px;margin-bottom:16px}}
 header.rpt img.logo{{max-height:84px;max-width:240px;object-fit:contain}}
 header.rpt .hmeta{{flex:1}}
 header.rpt h1{{margin:0 0 6px;font-size:20px}}
 header.rpt table{{border-collapse:collapse}}
 header.rpt td{{padding:2px 14px 2px 0;vertical-align:top}}
 header.rpt td:first-child{{color:var(--mut);white-space:nowrap}}
 header.rpt .hnotes{{margin-top:8px;white-space:pre-wrap;max-width:70ch}}
 .summary{{margin:18px 0 6px}}
 .summary .cap{{font-weight:700;font-size:12px;letter-spacing:.08em;text-transform:uppercase;
   color:var(--mut);margin-bottom:9px}}
 .summary .bar{{display:flex;height:9px;border:1px solid var(--line);border-radius:5px;
   overflow:hidden;margin-bottom:14px;max-width:620px}}
 .summary .bar i{{display:block;height:100%;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 .summary table{{border-collapse:collapse;font-size:12.5px}}
 .summary td{{padding:3px 0}}
 .summary td.lbl{{padding-right:30px}}
 .summary td.n{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;
   padding-right:22px;min-width:66px}}
 .summary td.pct{{text-align:right;font-variant-numeric:tabular-nums;color:var(--mut);min-width:50px}}
 .summary tr.grp td{{border-top:1px solid var(--line);padding-top:8px;color:var(--mut);
   font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;font-weight:700}}
 .summary tr.first td{{font-weight:700;font-size:13px;padding-bottom:5px}}
 .summary tr.sub td.lbl{{padding-left:16px;color:var(--mut)}}
 .summary tr.tot td{{border-top:2px solid var(--ink);padding-top:7px;font-weight:700}}
 .summary .sw{{display:inline-block;width:9px;height:9px;border-radius:2px;
   margin-right:9px;vertical-align:1px;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 .overview{{margin:14px 0 4px}}
 .overview .cap{{font-weight:700;font-size:12px;letter-spacing:.08em;text-transform:uppercase;
   color:var(--mut);margin-bottom:9px}}
 .overview img{{width:100%;max-width:900px;border:1px solid var(--line);border-radius:8px;
   display:block;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 .overview .ovnote{{color:var(--mut);font-size:12px;margin-top:6px}}
 nav.toc{{display:flex;flex-wrap:wrap;gap:7px;margin:16px 0 4px;align-items:center}}
 nav.toc .lbl{{color:var(--mut);font-size:12px;font-weight:600}}
 nav.toc a{{border:1px solid #2f6fd8;border-radius:6px;padding:3px 12px;text-decoration:none;
   color:#2f6fd8;font-size:12px;font-weight:600;background:#f0f5ff;transition:all .12s}}
 nav.toc a::before{{content:"\\2193 ";opacity:.7}}
 nav.toc a:hover{{background:#2f6fd8;color:#fff}}
 nav.toc a .n{{opacity:.7;font-weight:400}}
 .rptbar{{margin:12px 0 4px;font-size:12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
 .rptbar .lbl{{color:var(--mut);font-weight:600}}
 .rptbar button{{border:1px solid var(--line);background:#f7f8fa;border-radius:6px;
   padding:3px 10px;cursor:pointer;font:inherit;color:var(--ink)}}
 .rptbar button:hover{{border-color:#2f6fd8}}
 .rptbar .sep{{flex:0 0 10px}}
 .rptbar .tgl{{display:inline-flex;align-items:center;gap:8px;padding:3px 12px 3px 8px;font-weight:600}}
 .rptbar .tgl .sw{{width:32px;height:17px;border-radius:9px;background:#c3c8d0;position:relative;
   flex:0 0 auto;transition:background .15s}}
 .rptbar .tgl .sw::after{{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;
   border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.35);transition:left .15s}}
 .rptbar .tgl.on{{border-color:#2f6fd8;color:#2f6fd8}}
 .rptbar .tgl.on .sw{{background:#2f6fd8}}
 .rptbar .tgl.on .sw::after{{left:17px}}
 h2.catsec{{font-size:16px;margin:28px 0 10px;padding:5px 0 5px 12px;scroll-margin-top:8px;
   display:flex;align-items:baseline;gap:8px}}
 h2.catsec .n{{color:var(--mut);font-weight:400;font-size:13px}}
 h2.catsec .toplink{{margin-left:auto;font-size:11px;font-weight:600;text-decoration:none;
   color:#2f6fd8;border:1px solid var(--line);border-radius:5px;padding:1px 9px}}
 h2.catsec .toplink:hover{{background:#2f6fd8;color:#fff;border-color:#2f6fd8}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}}
 .card{{border:1px solid var(--line);border-radius:8px;overflow:hidden;break-inside:avoid}}
 .card .thumbwrap{{position:relative}}
 .card img{{width:100%;height:180px;object-fit:contain;background:#0c0d10;display:block}}
 .card .playicon{{position:absolute;inset:0;margin:auto;width:48px;height:48px;
   border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:20px;
   line-height:48px;text-align:center;pointer-events:none}}
 .card img.locmap{{height:150px;object-fit:cover;background:#e9e6df;
   border-top:1px solid var(--line);-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 html.blur .card img.locmap{{filter:none}}   /* a locator map carries no evidence imagery */
 .card .catbar{{padding:3px 9px;color:#fff;font-weight:600;font-size:11px}}
 .card details.meta{{font-size:12px}}
 .card details.meta > summary{{padding:7px 10px;cursor:pointer;font-weight:600;
   list-style:none;word-break:break-all}}
 .card details.meta > summary::-webkit-details-marker{{display:none}}
 .card details.meta > summary::before{{content:"\\25B8 ";color:var(--mut)}}
 .card details.meta[open] > summary::before{{content:"\\25BE "}}
 .card .fields{{padding:0 10px 8px}}
 .card .f{{display:flex;gap:6px;padding:1px 0}}
 .card .f .k{{color:var(--mut);flex:0 0 84px}}
 .card .f .v{{flex:1;word-break:break-all}}
 .card .f.mono .v{{font-family:ui-monospace,Consolas,monospace;font-size:11px}}
 .pill{{display:inline-block;padding:1px 7px;border-radius:10px;background:#eef0f3;margin:1px 1px 0 0}}
 /* dark mode (toggle) */
 html.dark{{--line:#333a46;--ink:#e6e8ec;--mut:#8b93a3}}
 html.dark body{{background:#14161a}}
 html.dark header.rpt{{border-bottom-color:#e6e8ec}}
 html.dark nav.toc a,html.dark .rptbar button,
 html.dark .card{{background:#1c1f26}}
 html.dark .pill{{background:#242833}}
 html.dark a{{color:#7fb0ff}}
 /* blur images (on by default; toggle off in the bar). Hover a thumbnail for a
    full, unblurred look; click it to keep that one revealed. */
 html.blur .card img{{filter:blur(22px)}}
 html.blur .card img:hover{{filter:none}}
 .card img.openable:hover{{outline:3px solid #2f6fd8;outline-offset:-3px}}
 @media print{{
   .wrap{{max-width:none;padding:0}} body{{margin:12mm}}
   .card{{page-break-inside:avoid}} .card img{{background:#fff;filter:none !important}}
   .rptbar{{display:none}}
   .card details.meta > summary::before{{content:""}}
   html.dark{{--line:#d5d9e0;--ink:#1a1d24;--mut:#5b6472}}
   html.dark body{{background:#fff;color:#1a1d24}}
   html.dark .card{{background:#fff}}
 }}
</style></head><body><a id="top"></a><div class="wrap">
"""


def _header_html(case: Case, header: dict | None) -> str:
    h = header or {}
    def g(k):
        return html.escape(str(h.get(k) or "")).replace("\n", "<br>")
    logo = h.get("logo")
    logo_html = ""
    if isinstance(logo, str) and logo.startswith("data:image/") and len(logo) < 4_000_000:
        logo_html = f"<img class='logo' src='{html.escape(logo, quote=True)}' alt=''>"
    case_name = html.escape(str(case.db.get_meta("case_name") or ""))
    rows = [("Agency", g("agency")), ("Case number", g("case_number")),
            ("Item number", g("item_number")),
            ("Examiner", g("examiner") or html.escape(case.examiner)),
            ("Generated", _fmt_ts(time.time())),
            ("Times shown in", html.escape(timeutil.label(_TZ)))]
    meta = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows if v)
    notes = f"<div class='hnotes'>{g('notes')}</div>" if h.get("notes") else ""
    tznote = ("<div class='hnotes' style='font-size:11px;color:#888'>"
              "Filesystem and ingest times are shown in the timezone above "
              "(daylight saving applied). &ldquo;Captured (EXIF)&rdquo; is the "
              "camera&rsquo;s own local time, shown exactly as recorded in the file."
              "</div>")
    return (f"<header class='rpt'>{logo_html}<div class='hmeta'>"
            f"<h1>{case_name or 'Media report'}</h1>"
            f"<table>{meta}</table>{notes}{tznote}</div></header>")


def _basemap_for_render() -> dict | None:
    """The active basemap as a record ready for ``staticmap``, or None if there
    is none or its file has gone. Reads nothing from the network."""
    name = basemaps.get_active()
    if not name:
        return None
    rec = basemaps.get(name)
    if not rec or not Path(rec["path"]).is_file():
        return None
    try:
        info = basemaps.inspect(rec["path"])
    except (OSError, ValueError):
        return None
    return {"path": rec["path"], "format": rec["format"],
            "tile_type": info.get("tile_type"),
            "min_zoom": info.get("min_zoom", 0), "max_zoom": info.get("max_zoom", 19)}


def _render_report_maps(case: Case, rows: list[dict], *, flavor: str, cap: int
                        ) -> tuple[str, dict[int, str], dict[str, int]]:
    """Draw the maps embedded in the HTML report from the active offline basemap.

    Returns the overview image (a data URI framing the files that were drawn), a
    ``{file_id: data URI}`` of per-file locator maps, and a tally of why the rest
    have none, which the Locations note reports: a map that is absent for a stated
    reason is a result, and a map that is silently absent is a gap the reader has to
    guess at. Everything is rendered locally; nothing is fetched.

    A point the basemap holds no tile for is skipped rather than drawn, the same as
    the LAVA export does, because it renders as the background colour with a pin on
    it, which reads as a location with nothing around it. Measured on a regional
    basemap at 360x240: a point outside its coverage drew a JPEG that was 91.9% one
    colour against 1.2% for a point inside it.
    """
    tally = {"drawn": 0, "no basemap": 0, "outside the basemap": 0,
             "over the cap": 0, "failed to draw": 0}
    geo = [d for d in rows if d.get("gps_lat") is not None and d.get("gps_lon") is not None]
    if not geo:
        return "", {}, tally
    rec = _basemap_for_render()
    if rec is None:
        tally["no basemap"] = len(geo)
        return "", {}, tally
    # this report was drawn on this basemap: record the name and hash for the summary
    try:
        basemaps.record_use(case, basemaps.get_active())
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # provenance is best-effort, never fatal
    cache: dict = {}
    per: dict[int, str] = {}
    covered: list[tuple] = []
    for index, d in enumerate(geo):
        if index >= cap:
            tally["over the cap"] = len(geo) - cap
            break
        lon, lat = d["gps_lon"], d["gps_lat"]
        # one tile read against a whole render: measured on a regional basemap at
        # 5.9 ms a point, against 76.1 ms to draw one
        if not staticmap.covers(rec, lon, lat):
            tally["outside the basemap"] += 1
            continue
        try:
            jpg = staticmap.render(rec, [(lon, lat)], width=360, height=240,
                                   flavor=flavor, cache=cache, fmt="jpeg")
        except Exception:  # pylint: disable=broad-exception-caught
            tally["failed to draw"] += 1
            continue
        per[d["id"]] = "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")
        covered.append((lon, lat))
        tally["drawn"] += 1
    overview = ""
    if covered:
        # framed on the drawn points only: one far away file the basemap cannot show
        # would otherwise zoom the overview out until the rest are a single dot
        try:
            png = staticmap.render(rec, covered, width=900, height=540, flavor=flavor,
                                   cache=cache, fmt="png")
            overview = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        except Exception:  # pylint: disable=broad-exception-caught
            overview = ""
    return overview, per, tally


def _overview_html(overview: str, tally: dict[str, int]) -> str:
    """The Locations section: the overview image, and what it does and does not hold.

    The note counts the geolocated files that got a map and the ones that did not,
    by reason, so a card with no locator map is accounted for rather than left to the
    reader to explain. With no basemap imported there is no section at all, which is
    the same as a report asked for no maps.
    """
    geo = sum(tally.values())
    if not geo or tally.get("no basemap"):
        return ""
    drawn = tally["drawn"]
    if drawn == geo:
        note = (f"{geo:,} geolocated file(s), drawn on the imported offline basemap. "
                "A per-file locator map appears on each geolocated file below.")
    else:
        left = ", ".join(f"{tally[k]:,} {k}" for k in
                         ("outside the basemap", "over the cap", "failed to draw")
                         if tally[k])
        note = (f"{geo:,} geolocated file(s): {drawn:,} drawn on the imported offline "
                f"basemap, {left}. ")
        note += ("A per-file locator map appears on each file that was drawn, and the "
                 "overview frames those files only." if drawn else
                 "There is no overview map, because none of them could be drawn on "
                 "this basemap.")
    img = (f"<img src='{html.escape(overview, quote=True)}' "
           f"alt='map of the geolocated files that could be drawn'>"
           if overview else "")
    return (f"<section class='overview'><div class='cap'>Locations</div>{img}"
            f"<div class='ovnote'>{html.escape(note)}</div></section>")


def _summary_html(case: Case, rows: list[dict], label: str) -> str:
    by_kind = Counter(d.get("kind") for d in rows)
    by_cat = Counter(d.get("category") or 0 for d in rows)
    hits = sum(1 for d in rows if d.get("hashset_hit"))
    n = len(rows)
    total = case.db.stats()["total"]
    cmap = categories.catmap(case.db)

    def line(cls: str, name: str, count: int, *, pct: str | None = None,
             swatch: str | None = None) -> str:
        sw = (f"<span class='sw' style='background:{html.escape(swatch, quote=True)}'></span>"
              if swatch else "")
        return (f"<tr class='{cls}'><td class='lbl'>{sw}{html.escape(str(name))}</td>"
                f"<td class='n'>{count:,}</td>"
                f"<td class='pct'>{pct or ''}</td></tr>")

    out = [line("first", "Files in this report", n)]
    for k, lbl in (("image", "Images"), ("video", "Videos"), ("other", "Other files")):
        if by_kind.get(k):
            out.append(line("sub", lbl, by_kind[k]))

    codes = sorted(by_cat, key=lambda c: (c == 0, cmap.get(c, {}).get("position", c), c))
    if codes:
        out.append("<tr class='grp'><td colspan='3'>By category</td></tr>")
        for c in codes:
            pct = f"{100 * by_cat[c] / n:.1f}%" if n else None
            out.append(line("cat", categories.label(case.db, c), by_cat[c],
                            pct=pct, swatch=categories.color(case.db, c)))

    if hits:
        out.append("<tr class='grp'><td colspan='3'>Known-hash</td></tr>")
        out.append(line("sub", "Files matching a known set", hits))

    out.append(line("tot", "Total media in the case", total))

    # which offline basemap the review's maps were drawn on, so a reader can obtain the
    # same file and see the same map
    bm_sha = case.db.get_meta("basemap_sha256")
    if bm_sha:
        bm = f"{case.db.get_meta('basemap_name') or ''} ({case.db.get_meta('basemap_format') or ''})"
        out.append("<tr class='grp'><td colspan='3'>Map</td></tr>")
        out.append(f"<tr class='sub'><td class='lbl'>Basemap used in review</td>"
                   f"<td colspan='2' style='font-size:11px'>{html.escape(bm)}<br>"
                   f"<span style='font-family:monospace'>sha256 {html.escape(bm_sha)}</span></td></tr>")

    bar = ""
    if n and len(codes) > 1:
        segs = "".join(
            f"<i style='width:{100 * by_cat[c] / n:.4f}%;"
            f"background:{html.escape(categories.color(case.db, c), quote=True)}' "
            f"title='{html.escape(categories.label(case.db, c))}: "
            f"{by_cat[c]:,} ({100 * by_cat[c] / n:.1f}%)'></i>"
            for c in codes)
        bar = f"<div class='bar'>{segs}</div>"

    scope = f" &mdash; {html.escape(label)}" if label else ""
    return (f"<section class='summary'>"
            f"<div class='cap'>Report contents{scope}</div>{bar}"
            f"<table>{''.join(out)}</table></section>")


def _view_source(case: Case, d: dict) -> Path:
    """The file to build the full-size view from: an LZC-extracted sidecar if
    one exists, else the ingested original."""
    ex = case.root / "extracted"
    if ex.is_dir():
        hit = next(ex.glob(f"{d['id']}.*"), None)
        if hit:
            return hit
    return Path(d["path"])


# .mov / .m4v are ISO-BMFF like .mp4 - label them video/mp4 so browsers that
# won't touch "video/quicktime" still try to play them
_VIDEO_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4",
               ".webm": "video/webm", ".ogv": "video/ogg", ".mkv": "video/x-matroska",
               ".avi": "video/x-msvideo", ".3gp": "video/3gpp"}


def _video_data_uri(path: Path, max_bytes: int = 60_000_000) -> str:
    """The video file itself as a data URI, or '' if it's missing or too big
    to reasonably inline."""
    p = Path(path)
    try:
        if p.stat().st_size > max_bytes:
            return ""
        raw = p.read_bytes()
    except OSError:
        return ""
    mime = _VIDEO_MIME.get(p.suffix.lower(), "video/mp4")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _fullview_candidates(case: Case, d: dict):
    """Files, best first, to build a click-to-open 'full size' image from."""
    if d.get("kind") == "video":
        kfs = case.db.conn.execute(
            "SELECT thumb FROM keyframes WHERE file_id=? AND thumb IS NOT NULL "
            "ORDER BY ts", (d["id"],)).fetchall()
        if kfs:
            yield case.thumb_dir / kfs[len(kfs) // 2]["thumb"]
        if d.get("thumb"):
            yield case.thumb_dir / d["thumb"]
        return
    yield _view_source(case, d)                       # the (LZC sidecar or) original
    v = case.root / "views" / f"{d['id']}.jpg"        # a cached transcode, if any
    if v.exists():
        yield v


def _fullview_jpeg(case: Case, d: dict, max_px: int = 2000) -> bytes | None:
    """A downscaled JPEG for click-to-open, or None if nothing works.

    Images: the original decoded and capped at max_px. Videos: the middle
    key frame. HEIC/HEIF work because ``imaging`` registers the decoder.
    """
    from PIL import Image
    for src in _fullview_candidates(case, d):
        try:
            with Image.open(src) as im:
                im.load()
                im = im.convert("RGB")
            if max(im.size) > max_px:
                im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            return buf.getvalue()
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    return None


def _fullview_data_uri(case: Case, d: dict, max_px: int = 2000) -> str:
    """``_fullview_jpeg`` as a data URI, or '' if nothing works."""
    raw = _fullview_jpeg(case, d, max_px)
    if not raw:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _card_html(case: Case, d: dict, keys: list[str], thumb_root: Path,
               full_images: bool, full_videos: bool, loc_map: str = "") -> str:
    code = d.get("category") or 0
    catbar = (f"<div class='catbar' style='background:"
              f"{html.escape(categories.color(case.db, code))}'>"
              f"{html.escape(d['category_label'])}</div>" if code else "")
    name = html.escape(_disp_name(d))
    is_video = d.get("kind") == "video"
    img = blob = ""
    if d.get("thumb"):
        src = _img_data_uri(thumb_root / d["thumb"])
        if src:
            attrs = f" data-name='{name}'"
            cls = "rimg" + (" video" if is_video else "")
            if is_video and full_videos:
                vuri = _video_data_uri(_view_source(case, d))
                if vuri:
                    attrs += f" data-video='v{d['id']}'"
                    cls += " openable"
                    blob = f"<script type='text/plain' id='v{d['id']}'>{vuri}</script>"
            if "openable" not in cls and full_images:
                full = _fullview_data_uri(case, d)          # image, or a key frame
                if full:
                    attrs += f' data-full="{full}"'
                    cls += " openable"
            play = "<span class='playicon'>&#9654;</span>" if is_video else ""
            img = f"<div class='thumbwrap'><img class='{cls}' src='{src}'{attrs}>{play}</div>{blob}"
    parts = []
    for k in keys:
        lbl, fn, mono = _FIELD_DEFS[k]
        try:
            val = fn(d)
        except Exception:  # noqa: BLE001 - a bad row must not kill the report
            val = ""
        if val:
            parts.append(f"<div class='f{' mono' if mono else ''}'>"
                         f"<span class='k'>{html.escape(lbl)}</span>"
                         f"<span class='v'>{html.escape(str(val))}</span></div>")
    locimg = (f"<img class='locmap' src='{html.escape(loc_map, quote=True)}' "
              f"alt='location of {name}' title='drawn on the imported offline basemap'>"
              if loc_map else "")
    return (f"<div class='card'>{img}{catbar}{locimg}"
            f"<details class='meta'><summary>{name}</summary>"
            f"<div class='fields'>{''.join(parts)}</div></details></div>")


def export_html(case: Case, dest: str | Path, where: str = "", *,
                thumbs: bool = True, header: dict | None = None,
                fields: list[str] | None = None, scope_label: str = "",
                full_images: bool = True, full_videos: bool = True,
                tz: str | None = None, maps: bool = True,
                map_flavor: str = "light", map_cap: int = 400) -> Path:
    global _TZ
    _TZ = tz
    dest = Path(dest)
    rows = _rows(case, where)
    keys = [k for k in (fields or DEFAULT_REPORT_FIELDS) if k in _FIELD_DEFS] \
        or DEFAULT_REPORT_FIELDS
    cn = html.escape(str(case.db.get_meta("case_name") or "GLEAPP"))
    thumb_root = case.thumb_dir

    # group by category, ordered by the category's display position (0 last)
    cmap = categories.catmap(case.db)
    groups: dict[int, list[dict]] = {}
    for d in rows:
        groups.setdefault(d.get("category") or 0, []).append(d)

    def _order(code: int) -> tuple:
        if code == 0:
            return (1, 1e9, 0)
        return (0, cmap.get(code, {}).get("position", code), code)
    codes = sorted(groups, key=_order)

    overview_uri, loc_maps, map_tally = ("", {}, {})
    if maps:
        overview_uri, loc_maps, map_tally = _render_report_maps(
            case, rows, flavor=map_flavor, cap=map_cap)

    body = [_HTML_HEAD.format(case=cn)]
    body.append(_header_html(case, header))
    body.append(_summary_html(case, rows, scope_label))
    body.append(_overview_html(overview_uri, map_tally))

    if len(codes) > 1:
        toc = "".join(
            f"<a href='#cat-{c}'>{html.escape(categories.label(case.db, c))} "
            f"<span class='n'>{len(groups[c]):,}</span></a>" for c in codes)
        body.append(f"<nav class='toc'><span class='lbl'>Jump to section:</span>{toc}</nav>")
    body.append(
        "<div class='rptbar'><span class='lbl'>Metadata:</span> "
        "<button type='button' onclick=\"document.querySelectorAll("
        "'details.meta').forEach(d=>d.open=true)\">expand all</button> "
        "<button type='button' onclick=\"document.querySelectorAll("
        "'details.meta').forEach(d=>d.open=false)\">collapse all</button>"
        "<span class='sep'></span>"
        "<button type='button' id='btnBlur' class='tgl'><span class='sw'></span>Blur images</button>"
        "<button type='button' id='btnDark' class='tgl'><span class='sw'></span>Dark mode</button></div>")

    for c in codes:
        lbl = html.escape(categories.label(case.db, c))
        col = html.escape(categories.color(case.db, c))
        body.append(
            f"<h2 class='catsec' id='cat-{c}' style='border-left:6px solid {col}'>"
            f"{lbl} <span class='n'>({len(groups[c]):,})</span>"
            f"<a class='toplink' href='#top'>&uarr; top</a></h2>")
        body.append("<div class='grid'>")
        for d in groups[c]:
            body.append(_card_html(case, d, keys, thumb_root,
                                   full_images, full_videos,
                                   loc_map=loc_maps.get(d["id"], "")))
        body.append("</div>")

    body.append(_REPORT_JS)
    body.append("</div></body></html>")
    dest.write_text("".join(body), encoding="utf-8")
    return dest


_REPORT_JS = """
<script>
(function(){
  var root = document.documentElement;
  function get(k){ try{ return localStorage.getItem('gleapp.report.'+k); }catch(e){ return null; } }
  function set(k,v){ try{ localStorage.setItem('gleapp.report.'+k, v); }catch(e){} }
  // cls is applied by default when def is true; a stored '0'/'1' overrides.
  function wire(id, cls, key, def){
    var btn = document.getElementById(id);
    if(!btn) return;
    var stored = get(key);
    var on = stored === null ? def : (stored === '1');
    root.classList.toggle(cls, on);
    btn.classList.toggle('on', on);
    btn.addEventListener('click', function(){
      var v = root.classList.toggle(cls);
      btn.classList.toggle('on', v);
      set(key, v ? '1' : '0');
    });
  }
  wire('btnBlur', 'blur', 'blur', true);   // images start blurred
  wire('btnDark', 'dark', 'dark', false);
  // click a thumbnail -> open the full-size image, or play the video, in a new tab
  function openImage(im){
    var w = window.open('', '_blank');
    if(!w) return;
    w.document.write('<!doctype html><body style="margin:0;background:#111;'+
      'display:flex;align-items:center;justify-content:center;min-height:100vh">'+
      '<img style="max-width:100%;max-height:100vh"></body>');
    w.document.close();
    w.document.title = (im.getAttribute('data-name')||'image').replace(/[<>]/g,'');
    w.document.querySelector('img').src = im.getAttribute('data-full');
  }
  function openVideo(im){
    var w = window.open('', '_blank');
    if(!w) return;
    var name = (im.getAttribute('data-name') || 'video').replace(/[<>"]/g,'');
    w.document.write('<!doctype html><title>'+name+'</title>'+
      '<body style="margin:0;background:#111;color:#cfd3da;font:14px system-ui;'+
      'display:flex;flex-direction:column;align-items:center;justify-content:center;'+
      'min-height:100vh;gap:14px;text-align:center;padding:16px">'+
      '<video controls autoplay style="max-width:100%;max-height:88vh"></video>'+
      '<div id="msg"></div></body>');
    w.document.close();
    var v = w.document.querySelector('video');
    // decode the embedded data: URI into a Blob (no size limit, allows seeking)
    fetch(document.getElementById(im.getAttribute('data-video')).textContent)
      .then(function(r){ return r.blob(); })
      .then(function(b){
        var url = URL.createObjectURL(b);
        v.src = url;
        v.addEventListener('error', function(){
          w.document.getElementById('msg').innerHTML =
            'The browser can\\'t decode this video (iPhone clips are usually '+
            '<b>HEVC / H.265</b>). On Windows install <b>HEVC Video Extensions</b> '+
            'from the Microsoft Store, or <a style="color:#7fb0ff" href="'+url+
            '" download="'+name+'">download the file</a> and open it in a media player.';
        });
      });
  }
  document.querySelectorAll('.card img.openable').forEach(function(im){
    var isv = !!im.getAttribute('data-video');
    im.style.cursor = 'zoom-in';
    im.title = isv ? 'play video in a new tab' : 'open full size in a new tab';
    im.addEventListener('click', function(){ isv ? openVideo(im) : openImage(im); });
  });
  // always show every metadata block when printing, then restore
  var snap = [];
  window.addEventListener('beforeprint', function(){
    snap = [];
    document.querySelectorAll('details.meta').forEach(function(d){
      snap.push(d.open); d.open = true;
    });
  });
  window.addEventListener('afterprint', function(){
    document.querySelectorAll('details.meta').forEach(function(d, i){
      d.open = snap[i];
    });
  });
})();
</script>
"""


def _img_data_uri(p: Path) -> str:
    try:
        raw = Path(p).read_bytes()
    except OSError:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
