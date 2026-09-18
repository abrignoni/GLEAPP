"""Report + export generation: CSV, JSON, HTML, and KML for geolocation."""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import math
import time
import zipfile
from collections import Counter
from pathlib import Path

from . import basemaps, categories, flags, imaging, staticmap, timeutil, vicdetails  # noqa: F401  (imaging: registers HEIF decoder)
from .case import Case

# overview map size - shared so the clickable overlay in _overview_html always
# lines up with what _render_report_maps actually asked staticmap.render for
_OVERVIEW_SIZE = (900, 540)

_CSV_FIELDS = [
    "id", "file_path", "disk_name", "source", "kind", "ext", "size",
    "created_dt", "ctime", "mtime", "atime",
    "md5", "sha1", "sha256", "phash", "width", "height", "duration",
    "gps_lat", "gps_lon", "camera", "faces", "skin_ratio",
    "category", "category_label", "triage", "flags_label",
    "hashset_hit", "hashset_cat", "hashset_kind", "hash_matches",
    "stack_id", "vstack_id", "cluster_id", "notes",
    "media_id", "orig_name", "orig_path", "mime", "origin", "recorded_times",
    "vic_record_media_id", "vic_series", "vic_flags", "vic_tags", "vic_exif",
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
        d["flags"] = [dict(fr) for fr in case.db.flags_for(d["id"])]
        d["flags_label"] = ", ".join(fr["name"] for fr in d["flags"])
        d["file_path"] = _disp_path(d)      # device path (VIC) or source path
        d["disk_name"] = _disk_name(d)      # the on-disk (MD5) name
        # the matched Project VIC hash-set record, as an object rather than text
        d["hashset_vic"] = vicdetails.parse(d.get("hashset_vic"))
        # every source that flagged the file, as a list and as readable text
        d["hashset_sources"] = _sources(d.get("hashset_sources"))
        d["hash_matches"] = matched_in(d["hashset_sources"])
        out.append(d)
    return out


_SOURCE_LABEL = {"vic": "Project VIC", "stash": "Hash stash",
                 "other": "Hash set", "good": "Known-good set"}


def _sources(raw) -> list[dict]:
    """``files.hashset_sources`` as a list; empty when absent or unreadable."""
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw) if raw else []
    except ValueError:
        return []
    return val if isinstance(val, list) else []


def matched_in(sources: list[dict]) -> str:
    """The sources that flagged a file, as one line: ``Project VIC (VICS set,
    category 2); Hash stash (category 2)``. The stash is never named, since its
    name is the label."""
    parts = []
    for x in sources:
        label = _SOURCE_LABEL.get(x.get("src"), "Hash set")
        detail = []
        if x.get("src") in ("vic", "other", "good") and x.get("name"):
            detail.append(str(x["name"]))
        if x.get("category"):
            detail.append(f"category {x['category']}")
        parts.append(label + (f" ({', '.join(detail)})" if detail else ""))
    return "; ".join(parts)


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
            # A FAT or exFAT volume stores a wall clock and no zone, so the three
            # columns above are empty for it and these are the only filesystem
            # times the file has. Rendered as text, the way the report renders
            # them, because no instant can be derived from them.
            d["recorded_times"] = _recorded(d)
            # The file's own Project VIC values where a VIC import gave it some,
            # else those of the Project VIC hash-set record it matched, as text.
            vv = vicdetails.view(d)
            d.update(vic_record_media_id=vv["media_id"], vic_series=vv["series"],
                     vic_flags=vv["flags"], vic_tags=vv["tags"], vic_exif=vv["exif"])
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


# The heading for the readings a filesystem stored with no zone. One string,
# because the HTML report and the LAVA artifacts all show this column and a
# reader comparing them should not have to work out it is the same field.
RECORDED_LABEL = "Recorded (as stored, no zone)"


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
    "recorded_times": (RECORDED_LABEL, _recorded, False),
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
    "flags":      ("Flags",         lambda d: d.get("flags_label") or "", False),
    "notes":      ("Notes",         lambda d: d.get("notes") or "", False),
    "faces":      ("Faces",         lambda d: str(d["faces"]) if d.get("faces") else "", False),
    "skin_ratio": ("Skin ratio",    lambda d: f"{d['skin_ratio']:.2f}" if d.get("skin_ratio") else "", False),
    "source":     ("Source",        lambda d: d.get("source") or "", False),
    "origin":     ("How recovered", _origin_label, False),
    "mime":       ("MIME type",     lambda d: d.get("mime") or "", False),
    "media_id":   ("VIC MediaID",   lambda d: str(d["media_id"]) if d.get("media_id") is not None else "", False),
    "hashset":    ("Known hash",    lambda d: d.get("hashset_hit") or "", False),
    "hash_matches": ("Hash matches", lambda d: d.get("hash_matches") or "", False),
    # A Project VIC value: the file's own where a VIC import gave it one, else
    # the one on the Project VIC hash-set record the file matched.
    "vic_record": ("VIC record MediaID", lambda d: vicdetails.view(d)["media_id"], False),
    "vic_series": ("VIC series",    lambda d: vicdetails.view(d)["series"], False),
    "vic_flags":  ("VIC flags",     lambda d: vicdetails.view(d)["flags"], False),
    "vic_tags":   ("VIC tags",      lambda d: vicdetails.view(d)["tags"], False),
    "vic_exif":   ("VIC Exif (as recorded)", lambda d: vicdetails.view(d)["exif"], False),
    "error":      ("Error",         lambda d: d.get("error") or "", False),
}
DEFAULT_REPORT_FIELDS = ["name", "created_dt", "md5", "hash_matches", "vic_record", "vic_series",
                         "vic_flags", "vic_tags", "vic_exif"]

_HTML_HEAD = """<!doctype html><html class="{blur_cls}"><head><meta charset="utf-8">
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
 header.rpt img.logo{{max-height:110px;max-width:320px;width:auto;height:auto;
   object-fit:contain;display:block}}
 header.rpt .hmeta{{flex:1}}
 header.rpt h1{{margin:0 0 6px;font-size:20px}}
 header.rpt table{{border-collapse:collapse}}
 header.rpt td{{padding:2px 14px 2px 0;vertical-align:top}}
 header.rpt td:first-child{{color:var(--mut);white-space:nowrap}}
 header.rpt .hnotes{{margin-top:8px;white-space:pre-wrap;max-width:70ch}}
 .summary{{margin:18px 0 6px;border:1px solid var(--line);border-left:4px solid #2f6fd8;
   border-radius:0 8px 8px 0;overflow:hidden}}
 details.summary > summary.cap{{cursor:pointer;list-style:none;font-weight:700;font-size:14px;
   padding:11px 14px;color:var(--ink);background:#fafbfd;display:flex;align-items:center;
   gap:9px;user-select:none;transition:background .12s}}
 details.summary > summary.cap::-webkit-details-marker{{display:none}}
 details.summary > summary.cap::before{{content:"\\25B8";color:#2f6fd8;font-size:13px}}
 details.summary[open] > summary.cap::before{{content:"\\25BE"}}
 details.summary > summary.cap:hover{{background:#eef1f5}}
 html.dark details.summary > summary.cap{{background:#1c1f26}}
 html.dark details.summary > summary.cap:hover{{background:#242833}}
 .summary .sumrow{{display:flex;gap:32px;align-items:center;flex-wrap:wrap;padding:14px}}
 .summary .chartcol{{flex:0 0 auto}}
 .summary .donut{{width:148px;height:148px;display:block}}
 .summary .donut circle.ring{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 .summary .donut .donut-n{{font-size:7.5px;font-weight:700;fill:var(--ink)}}
 .summary .donut .donut-lbl{{font-size:3.4px;fill:var(--mut);text-transform:uppercase;letter-spacing:.06em}}
 .summary .datacol{{flex:1;min-width:260px}}
 .summary table{{border-collapse:collapse;font-size:12.5px}}
 .summary td{{padding:3px 0}}
 .summary td.lbl{{padding-right:30px}}
 .summary td.lbl a{{color:inherit;text-decoration:none;border-bottom:1px dotted var(--mut)}}
 .summary td.lbl a:hover{{color:#2f6fd8;border-bottom-color:#2f6fd8}}
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
 details.overview{{margin:18px 0 4px;border:1px solid var(--line);border-left:4px solid #2f6fd8;
   border-radius:0 8px 8px 0;overflow:hidden}}
 details.overview > summary{{cursor:pointer;font-weight:700;font-size:14px;list-style:none;
   padding:11px 14px;color:var(--ink);background:#fafbfd;display:flex;align-items:center;
   gap:9px;user-select:none;transition:background .12s}}
 details.overview > summary::-webkit-details-marker{{display:none}}
 details.overview > summary::before{{content:"\\25B8";color:#2f6fd8;font-size:13px}}
 details.overview[open] > summary::before{{content:"\\25BE"}}
 details.overview > summary:hover{{background:#eef1f5}}
 details.overview > summary .n{{color:var(--mut);font-weight:400;font-size:13px}}
 details.overview > summary .hint{{margin-left:auto;font-size:11px;font-weight:600;
   color:#2f6fd8}}
 details.overview > .body{{padding:14px}}
 details.overview img,details.overview svg.ovsvg{{width:100%;max-width:900px;
   border:1px solid var(--line);border-radius:8px;display:block;
   -webkit-print-color-adjust:exact;print-color-adjust:exact}}
 details.overview svg.ovsvg a{{cursor:pointer}}
 details.overview svg.ovsvg .clusterdot{{fill:#2f6fd8;fill-opacity:.85;stroke:#fff;
   stroke-width:2;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 details.overview svg.ovsvg .cluster:hover .clusterdot,
 details.overview svg.ovsvg .cluster:focus-within .clusterdot{{fill-opacity:1}}
 details.overview svg.ovsvg .clustern{{font:700 13px system-ui,sans-serif;fill:#fff;
   pointer-events:none;user-select:none}}
 details.overview svg.ovsvg .petal{{opacity:0;pointer-events:none;transform-box:view-box;
   transform:scale(0);
   /* a short delay before collapsing (none going the other way) forgives the
      pointer briefly overshooting onto empty space on the way to a petal */
   transition:transform .18s ease .12s,opacity .18s ease .12s}}
 details.overview svg.ovsvg .cluster:hover .petal,
 details.overview svg.ovsvg .cluster:focus-within .petal{{
   transform:scale(1);opacity:1;
   transition:transform .12s ease,opacity .12s ease}}
 details.overview svg.ovsvg .petal .spoke{{stroke:#2f6fd8;stroke-width:1.5;pointer-events:none}}
 details.overview svg.ovsvg .petal .petaldot{{fill:#2f6fd8;stroke:#fff;stroke-width:1.5;
   pointer-events:none;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 details.overview svg.ovsvg .cluster:hover .petal .petaldot,
 details.overview svg.ovsvg .cluster:focus-within .petal .petaldot{{pointer-events:auto}}
 details.overview .ovnote{{color:var(--mut);font-size:12px;margin-top:6px}}
 details.overview .ovlinkscap{{font-weight:700;font-size:11px;letter-spacing:.06em;
   text-transform:uppercase;color:var(--mut);margin-top:12px;margin-bottom:6px}}
 details.overview .ovlinks{{max-height:160px;overflow-y:auto;border:1px solid var(--line);
   border-radius:6px;padding:8px 10px;display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px}}
 details.overview .ovlinks a{{color:#2f6fd8;text-decoration:none}}
 details.overview .ovlinks a:hover{{text-decoration:underline}}
 details.overview .ovbasemap{{color:var(--mut);font-size:11px;margin-top:10px;
   padding-top:8px;border-top:1px solid var(--line)}}
 html.dark details.overview > summary{{background:#1c1f26}}
 html.dark details.overview > summary:hover{{background:#242833}}
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
 details.kindsec{{margin:14px 0}}
 details.flagsec{{margin-left:18px}}   /* level with a category heading's own text (6px border + 12px padding) */
 details.kindsec > summary{{cursor:pointer;font-weight:700;font-size:13px;
   list-style:none;padding:4px 0;color:var(--ink)}}
 details.kindsec > summary::-webkit-details-marker{{display:none}}
 details.kindsec > summary::before{{content:"\\25B8 ";color:var(--mut);display:inline-block;width:14px}}
 details.kindsec[open] > summary::before{{content:"\\25BE "}}
 details.kindsec > summary .n{{color:var(--mut);font-weight:400;margin-left:4px}}
 details.kindsec > .grid{{margin-top:10px}}
 details.flagsec > summary .fsw{{display:inline-block;width:9px;height:9px;border-radius:2px;
   margin-right:5px;vertical-align:1px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}}
 .card{{border:1px solid var(--line);border-radius:8px;overflow:hidden;break-inside:avoid;
   scroll-margin-top:12px}}
 .card:target{{outline:3px solid #2f6fd8;outline-offset:1px}}
 .card .thumbwrap{{position:relative}}
 .card img{{width:100%;height:180px;object-fit:contain;background:#0c0d10;display:block}}
 .card .playicon{{position:absolute;inset:0;margin:auto;width:48px;height:48px;
   border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:20px;
   line-height:48px;text-align:center;pointer-events:none}}
 .card img.locmap{{height:150px;object-fit:cover;background:#e9e6df;
   border-top:1px solid var(--line);-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 /* the map is inside the metadata drop and should stay hidden with it - the
    explicit display:block above (needed while open) outranks the browser's own
    rule for a closed <details>, so it has to be re-stated here */
 .card details.meta:not([open]) img.locmap{{display:none}}
 html.blur .card img.locmap{{filter:none}}   /* a locator map carries no evidence imagery */
 .card .catbar{{padding:3px 9px;color:#fff;font-weight:600;font-size:11px}}
 .card .flagrow{{padding:5px 9px 0}}
 .card .rchip{{display:inline-flex;align-items:center;gap:5px;background:#f1f2f5;
   border:1px solid var(--line);border-radius:10px;padding:1px 8px 1px 5px;
   margin:0 4px 4px 0;font-size:11px}}
 .card .rchip i{{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block}}
 html.dark .card .rchip{{background:#242833}}
 .card details.meta{{font-size:12px}}
 .card details.meta > summary{{padding:7px 10px;cursor:pointer;font-weight:600;
   list-style:none;word-break:break-all}}
 .card details.meta > summary::-webkit-details-marker{{display:none}}
 .card details.meta > summary::before{{content:"\\25B8 ";color:var(--mut)}}
 .card details.meta[open] > summary::before{{content:"\\25BE "}}
 .card .fields{{padding:0 10px 8px}}
 .card .f{{display:flex;gap:6px;padding:1px 0}}
 .card .f .k{{color:var(--mut);flex:0 0 84px}}
 .card .f .v{{flex:1;word-break:break-all;white-space:pre-line}}
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
   details.kindsec > summary::before{{content:""}}
   details.overview{{border:none}}
   details.overview > summary{{background:none !important;padding:4px 0;font-size:12px;
     text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}}
   details.overview > summary::before{{content:""}}
   details.overview > summary .hint{{display:none}}
   details.summary{{border:none}}
   details.summary > summary.cap{{background:none !important;padding:4px 0;font-size:12px;
     text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}}
   details.summary > summary.cap::before{{content:""}}
   .summary .sumrow{{padding:0}}
   /* no hover on paper - show every clustered file fanned out, permanently */
   details.overview svg.ovsvg .petal{{opacity:1 !important;transform:none !important;
     pointer-events:auto}}
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
    return (f"<header class='rpt'>{logo_html}<div class='hmeta'>"
            f"<h1>{case_name or 'Media report'}</h1>"
            f"<table>{meta}</table>{notes}</div></header>")


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
                        ) -> tuple[str, dict[int, str], dict[str, int], list[dict],
                                   dict[int, tuple[float, float]]]:
    """Draw the maps embedded in the HTML report from the active offline basemap.

    Returns the overview image (a data URI framing the files that were drawn), a
    ``{file_id: data URI}`` of per-file locator maps, a tally of why the rest have
    none (the Locations note reports it: a map that is absent for a stated reason
    is a result, and a map that is silently absent is a gap the reader has to guess
    at), the rows that were actually drawn (parallel to the returned marker
    positions), and a ``{file_id: (x, y)}`` of where each one's marker landed on
    the overview image, so it can be wired up as a clickable overlay (see
    _cluster_svg_markers). Everything is rendered locally; nothing is fetched.

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
        return "", {}, tally, [], {}
    rec = _basemap_for_render()
    if rec is None:
        tally["no basemap"] = len(geo)
        return "", {}, tally, [], {}
    # this report was drawn on this basemap: record the name and hash for the summary
    try:
        basemaps.record_use(case, basemaps.get_active())
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # provenance is best-effort, never fatal
    cache: dict = {}
    per: dict[int, str] = {}
    covered: list[tuple] = []
    drawn_rows: list[dict] = []
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
        drawn_rows.append(d)
        tally["drawn"] += 1
    overview = ""
    marker_px: dict[int, tuple[float, float]] = {}
    if covered:
        # framed on the drawn points only: one far away file the basemap cannot show
        # would otherwise zoom the overview out until the rest are a single dot
        try:
            png, px = staticmap.render(rec, covered, width=_OVERVIEW_SIZE[0],
                                       height=_OVERVIEW_SIZE[1], flavor=flavor,
                                       cache=cache, fmt="png", return_points=True)
            overview = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            marker_px = {d["id"]: xy for d, xy in zip(drawn_rows, px)}
        except Exception:  # pylint: disable=broad-exception-caught
            overview = ""
    return overview, per, tally, drawn_rows, marker_px


# the marker itself is drawn with radius 7 in staticmap.render, so points landing
# closer together than this already read as one blob on the raster underneath
_CLUSTER_PX = 7


def _cluster_svg_markers(geo_rows: list[dict], marker_px: dict[int, tuple[float, float]]) -> str:
    """The clickable overlay for the overview map's markers, as SVG.

    A location with only one file gets a plain link, same as before. Two or more
    files at (near enough) the same point get a numbered cluster dot that fans its
    files out around it on hover or keyboard focus, the way a map app spreads out
    a pin stack - Google Earth is the example the report was asked to match.
    Only the fanned-out copy is reachable by a mouse click at rest, so the plain
    click-a-name list stays as the way to reach any file without hovering first.
    """
    # a fixed pixel grid would cluster two points 2px apart differently depending
    # on whether they straddle a grid line - a real "is this within _CLUSTER_PX"
    # test (union-find over every pair within range) doesn't have that seam
    items = [d for d in geo_rows if d["id"] in marker_px]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        xi, yi = marker_px[items[i]["id"]]
        for j in range(i + 1, len(items)):
            xj, yj = marker_px[items[j]["id"]]
            if (xi - xj) ** 2 + (yi - yj) ** 2 <= _CLUSTER_PX ** 2:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    buckets: dict[int, list[dict]] = {}
    for i, d in enumerate(items):
        buckets.setdefault(find(i), []).append(d)

    parts = []
    for items in buckets.values():
        cx, cy = marker_px[items[0]["id"]]
        if len(items) == 1:
            d = items[0]
            nm = html.escape(_disp_name(d))
            parts.append(f"<a href='#file-{d['id']}'><circle cx='{cx:.1f}' cy='{cy:.1f}' "
                        f"r='12' fill='transparent'><title>{nm}</title></circle></a>")
            continue
        n = len(items)
        radius = min(70, 24 + 6 * (n - 1))          # fan spreads wider for more files, capped
        petals = []
        for i, d in enumerate(items):
            angle = 2 * math.pi * i / n - math.pi / 2   # first petal points straight up
            fx, fy = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            nm = html.escape(_disp_name(d))
            # each petal is its own group, scaled from zero at the shared centre
            # (cx,cy) up to full size - the line and the dot collapse to that one
            # point together, so a line to the number is what's actually shrinking
            # away rather than a separate effect.
            petals.append(
                f"<g class='petal' style='transform-origin:{cx:.1f}px {cy:.1f}px'>"
                f"<line class='spoke' x1='{cx:.1f}' y1='{cy:.1f}' x2='{fx:.1f}' y2='{fy:.1f}'></line>"
                f"<a href='#file-{d['id']}'><circle class='petaldot' cx='{fx:.1f}' cy='{fy:.1f}' r='9'>"
                f"<title>{nm}</title></circle></a></g>")
        # moving from the centre to one petal, or from one petal to another, has
        # to cross empty SVG space either way - a line only bridges its own petal,
        # not the arc between two of them. One always-on, invisible disk sized to
        # the fan's own footprint (never any bigger) keeps the whole cluster
        # hovered anywhere inside it, so the fan stays open while the pointer
        # travels around it, without reaching into another cluster's dot.
        zone_r = radius + 9 + 6
        parts.append(
            f"<g class='cluster'>"
            f"<circle class='clusterzone' cx='{cx:.1f}' cy='{cy:.1f}' r='{zone_r}' "
            f"fill='transparent'></circle>"
            f"<circle class='clusterdot' cx='{cx:.1f}' cy='{cy:.1f}' r='13'>"
            f"<title>{n} files here - hover to choose one</title></circle>"
            f"<text class='clustern' x='{cx:.1f}' y='{cy + 4.5:.1f}' text-anchor='middle'>{n}</text>"
            f"{''.join(petals)}</g>")
    return "".join(parts)


def _overview_html(case: Case, overview: str, tally: dict[str, int], drawn_rows: list[dict],
                   marker_px: dict[int, tuple[float, float]] | None = None,
                   overview_size: tuple[int, int] = _OVERVIEW_SIZE) -> str:
    """The Locations section: the overview image, and what it does and does not hold.

    The note counts the geolocated files that got a map and the ones that did not,
    by reason, so a card with no locator map is accounted for rather than left to the
    reader to explain. With no basemap imported there is no section at all, which is
    the same as a report asked for no maps.
    """
    geo = sum(tally.values())
    if not geo or tally.get("no basemap"):
        return ""
    marker_px = marker_px or {}
    ow, oh = overview_size
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

    # each marker becomes a clickable point: an inline SVG with the raster map as
    # its <image> and a transparent <a><circle> laid exactly on top of each marker,
    # at the same coordinates staticmap.render drew it at (or, where several files
    # share a point, a numbered cluster that fans them out on hover - see
    # _cluster_svg_markers). Unlike an HTML image map (<area coords=...>), an
    # SVG's own coordinate system scales with it, so the hit targets track the
    # markers at any display size with no extra code.
    areas = _cluster_svg_markers(drawn_rows, marker_px) if overview else ""
    if areas:
        note += " Click a point on the map, or a name below, to jump to that file."
    if "class='cluster'" in areas:
        note += " Hover (or tab to) a numbered point to pick from the files sharing that spot."

    if areas:
        mapimg = (f"<svg class='ovsvg' viewBox='0 0 {ow} {oh}' role='img' "
                 f"aria-label='map of the geolocated files that could be drawn'>"
                 f"<image href='{html.escape(overview, quote=True)}' "
                 f"width='{ow}' height='{oh}'></image>{areas}</svg>")
    elif overview:
        mapimg = (f"<img src='{html.escape(overview, quote=True)}' "
                 f"alt='map of the geolocated files that could be drawn'>")
    else:
        mapimg = ""

    # a text jump-list alongside the clickable map: two files can sit at the same
    # coordinate (only one is reachable as a map point), a name is easier to
    # click precisely than a small dot, and it still works if the map image
    # itself failed to render for some reason
    links = "".join(
        f"<a href='#file-{d['id']}'>{html.escape(_disp_name(d))}</a>"
        for d in sorted(drawn_rows, key=lambda d: _disp_name(d).lower()))
    linklist = (f"<div class='ovlinkscap'>Jump to a file</div>"
               f"<div class='ovlinks'>{links}</div>") if links else ""

    # which offline basemap the review's maps were drawn on, so a reader can obtain
    # the same file and see the same map - shown under the map itself, not the sha256
    # (that stays out of the report; a reader who needs it has the case)
    bm_html = ""
    if case.db.get_meta("basemap_sha256"):
        bm = f"{case.db.get_meta('basemap_name') or ''} ({case.db.get_meta('basemap_format') or ''})"
        bm_html = (f"<div class='ovbasemap'>Basemap used in review: "
                   f"{html.escape(bm)}</div>")

    return (f"<details class='overview'><summary>Locations "
            f"<span class='n'>({geo:,})</span>"
            f"<span class='hint'>click to view map ▾</span></summary>"
            f"<div class='body'>{mapimg}"
            f"<div class='ovnote'>{html.escape(note)}</div>{linklist}{bm_html}</div></details>")


def _summary_html(case: Case, rows: list[dict], label: str, *,
                  flag_mode: bool = False) -> str:
    by_kind = Counter(d.get("kind") for d in rows)
    by_cat = Counter(d.get("category") or 0 for d in rows)
    by_flag: Counter = Counter()
    flagged = 0
    for d in rows:
        fl = d.get("flags") or []
        if fl:
            flagged += 1
        for f in fl:
            by_flag[f["code"]] += 1
    hits = sum(1 for d in rows if d.get("hashset_hit"))
    n = len(rows)
    total = case.db.stats()["total"]
    cmap = categories.catmap(case.db)
    fmap = flags.flagmap(case.db)

    def line(cls: str, name: str, count: int, *, pct: str | None = None,
             swatch: str | None = None, href: str | None = None) -> str:
        sw = (f"<span class='sw' style='background:{html.escape(swatch, quote=True)}'></span>"
              if swatch else "")
        label_html = (f"<a href='{html.escape(href, quote=True)}'>{html.escape(str(name))}</a>"
                      if href else html.escape(str(name)))
        return (f"<tr class='{cls}'><td class='lbl'>{sw}{label_html}</td>"
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
            # category sections only exist in the default (non-flag) report
            out.append(line("cat", categories.label(case.db, c), by_cat[c],
                            pct=pct, swatch=categories.color(case.db, c),
                            href=None if flag_mode else f"#cat-{c}"))

    flag_codes = sorted(by_flag, key=lambda c: fmap.get(c, {}).get("position", c))
    if flag_codes:
        out.append("<tr class='grp'><td colspan='3'>By flag</td></tr>")
        for c in flag_codes:
            pct = f"{100 * by_flag[c] / n:.1f}%" if n else None
            # every flag gets a #flag-{code} anchor whichever mode the report
            # is in - the top-level heading in a flags-only report, or the
            # first place that flag appears in the default (by-category) one
            out.append(line("cat", fmap.get(c, {}).get("name", f"Flag {c}"), by_flag[c],
                            pct=pct, swatch=fmap.get(c, {}).get("color"),
                            href=f"#flag-{c}"))
        pct_flagged = f"{100 * flagged / n:.1f}%" if n else None
        out.append(line("sub", "Files with at least one flag", flagged, pct=pct_flagged))

    if hits:
        out.append("<tr class='grp'><td colspan='3'>Known-hash</td></tr>")
        out.append(line("sub", "Files matching a known set", hits))

    out.append(line("tot", "Total media in the case", total))

    chart = _donut_svg(case, by_cat, codes, n)
    chartcol = f"<div class='chartcol'>{chart}</div>" if chart else ""

    scope = f" &mdash; {html.escape(label)}" if label else ""
    return (f"<details class='summary'>"
            f"<summary class='cap'>Report contents{scope}</summary>"
            f"<div class='sumrow'>{chartcol}"
            f"<div class='datacol'><table>{''.join(out)}</table></div></div></details>")


def _donut_svg(case: Case, by_cat: Counter, codes: list[int], n: int) -> str:
    """A ring chart of the category breakdown, drawn as plain stroked SVG
    circles (the classic percent-as-circumference trick) - no chart library,
    since the report is a single offline file."""
    if not n or not codes:
        return ""
    r = 15.91549430918954              # circumference == 100, so a percent is a percent
    segs = []
    cum = 0.0
    for c in codes:
        pct = 100 * by_cat[c] / n
        if pct <= 0:
            continue
        color = html.escape(categories.color(case.db, c), quote=True)
        title = (f"{html.escape(categories.label(case.db, c))}: "
                 f"{by_cat[c]:,} ({pct:.1f}%)")
        segs.append(
            f"<circle class='ring' cx='21' cy='21' r='{r}' fill='none' "
            f"stroke='{color}' stroke-width='8' "
            f"stroke-dasharray='{pct:.4f} {100 - pct:.4f}' "
            f"stroke-dashoffset='{-cum:.4f}'><title>{title}</title></circle>")
        cum += pct
    return (
        "<svg viewBox='0 0 42 42' class='donut' role='img' aria-label='category breakdown'>"
        "<g transform='rotate(-90 21 21)'>"
        f"<circle cx='21' cy='21' r='{r}' fill='none' stroke='var(--line)' stroke-width='8'></circle>"
        f"{''.join(segs)}"
        "</g>"
        f"<text x='21' y='19.5' text-anchor='middle' class='donut-n'>{n:,}</text>"
        "<text x='21' y='25.5' text-anchor='middle' class='donut-lbl'>files</text>"
        "</svg>")


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
    # Flags sit right under the category bar - small and colored, never
    # confusable with the (bold, singular) category itself. A file can carry
    # any number, including zero.
    flagrow = ""
    if d.get("flags"):
        chips = "".join(
            f"<span class='rchip'><i style='background:"
            f"{html.escape(fl['color'], quote=True)}'></i>{html.escape(fl['name'])}</span>"
            for fl in d["flags"])
        flagrow = f"<div class='flagrow'>{chips}</div>"
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
    # the locator map sits inside the metadata drop, not under the thumbnail,
    # so it only renders once the examiner opens that file's details. It reuses
    # the same click-to-open-full-size wiring as the main thumbnail (openImage
    # only ever reads data-full, never re-renders anything) - the "full size" is
    # the same 360x240 render the card crops to 150px tall, opened uncropped.
    locimg = (f"<img class='locmap openable' src='{html.escape(loc_map, quote=True)}' "
              f"data-full='{html.escape(loc_map, quote=True)}' data-name='location of {name}' "
              f"alt='location of {name}' title='drawn on the imported offline basemap'>"
              if loc_map else "")
    return (f"<div class='card' id='file-{d['id']}'>{img}{catbar}{flagrow}"
            f"<details class='meta'><summary>{name}</summary>{locimg}"
            f"<div class='fields'>{''.join(parts)}</div></details></div>")


_KIND_LABELS = (("image", "Images"), ("video", "Videos"), ("other", "Other files"))


def _by_kind(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split a section's rows into (label, rows) buckets - Images, Videos,
    Other - in that fixed order, skipping whichever are empty."""
    buckets: dict[str, list[dict]] = {}
    for d in items:
        k = d.get("kind") if d.get("kind") in ("image", "video") else "other"
        buckets.setdefault(k, []).append(d)
    return [(lbl, buckets[k]) for k, lbl in _KIND_LABELS if buckets.get(k)]


def export_html(case: Case, dest: str | Path, where: str = "", *,
                thumbs: bool = True, header: dict | None = None,
                fields: list[str] | None = None, scope_label: str = "",
                full_images: bool = True, full_videos: bool = True,
                tz: str | None = None, maps: bool = True,
                map_flavor: str = "light", map_cap: int = 400,
                blur: bool = True, by_flag: bool = False,
                only_flags: list[int] | None = None) -> Path:
    global _TZ
    _TZ = tz
    dest = Path(dest)
    rows = _rows(case, where)
    keys = [k for k in (fields or DEFAULT_REPORT_FIELDS) if k in _FIELD_DEFS] \
        or DEFAULT_REPORT_FIELDS
    cn = html.escape(str(case.db.get_meta("case_name") or "GLEAPP"))
    thumb_root = case.thumb_dir
    cmap = categories.catmap(case.db)
    fmap = flags.flagmap(case.db)

    overview_uri, loc_maps, map_tally, drawn_rows, marker_px = ("", {}, {}, [], {})
    if maps:
        overview_uri, loc_maps, map_tally, drawn_rows, marker_px = _render_report_maps(
            case, rows, flavor=map_flavor, cap=map_cap)

    body = [_HTML_HEAD.format(case=cn, blur_cls="blur" if blur else "")]
    body.append(_header_html(case, header))
    body.append(_summary_html(case, rows, scope_label, flag_mode=by_flag))
    body.append(_overview_html(case, overview_uri, map_tally, drawn_rows, marker_px))
    rptbar_html = (
        "<div class='rptbar'><span class='lbl'>Metadata:</span> "
        "<button type='button' onclick=\"document.querySelectorAll("
        "'details.meta').forEach(d=>d.open=true)\">expand all</button> "
        "<button type='button' onclick=\"document.querySelectorAll("
        "'details.meta').forEach(d=>d.open=false)\">collapse all</button>"
        "<span class='sep'></span>"
        "<button type='button' id='btnBlur' class='tgl'><span class='sw'></span>Blur images</button>"
        "<button type='button' id='btnDark' class='tgl'><span class='sw'></span>Dark mode</button></div>")

    def _kind_grids(items: list[dict]) -> None:
        """One collapsible Images/Videos/Other grid per kind present."""
        for klabel, sub in _by_kind(items):
            body.append(
                f"<details class='kindsec' open><summary>{klabel} "
                f"<span class='n'>({len(sub):,})</span></summary><div class='grid'>")
            for d in sub:
                body.append(_card_html(case, d, keys, thumb_root,
                                       full_images, full_videos,
                                       loc_map=loc_maps.get(d["id"], "")))
            body.append("</div></details>")

    def _section(label: str, items: list[dict], *, swatch: str = "", anchor: str = "") -> None:
        sw = (f"<span class='fsw' style='background:"
              f"{html.escape(swatch, quote=True)}'></span>" if swatch else "")
        id_attr = f" id='{anchor}'" if anchor else ""
        body.append(
            f"<details class='kindsec flagsec'{id_attr} open><summary>{sw}{label} "
            f"<span class='n'>({len(items):,})</span></summary>")
        _kind_grids(items)
        body.append("</details>")

    if by_flag:
        # A flags-only report: every row here already carries at least one
        # flag (the scope that asks for this filters to just those), and a
        # flag stands in for a category as the report's top-level section -
        # same heading (size, "jump to section" link, left color bar) a
        # category normally gets. Not mutually exclusive, so a file carrying
        # two flags appears once under each - unless the examiner picked
        # specific flags to include, in which case any *other* flag a
        # qualifying file also carries is left out of the grouping entirely.
        allowed = set(only_flags) if only_flags else None
        flag_groups: dict[int, list[dict]] = {}
        for d in rows:
            for fl in d.get("flags") or []:
                if allowed is not None and fl["code"] not in allowed:
                    continue
                flag_groups.setdefault(fl["code"], []).append(d)
        flag_codes = sorted(flag_groups, key=lambda fc: fmap.get(fc, {}).get("position", fc))

        if len(flag_codes) > 1:
            toc = "".join(
                f"<a href='#flag-{fc}'>{html.escape(fmap.get(fc, {}).get('name', f'Flag {fc}'))} "
                f"<span class='n'>{len(flag_groups[fc]):,}</span></a>" for fc in flag_codes)
            body.append(f"<nav class='toc'><span class='lbl'>Jump to section:</span>{toc}</nav>")
        body.append(rptbar_html)

        for fc in flag_codes:
            finfo = fmap.get(fc, {})
            lbl = html.escape(finfo.get("name", f"Flag {fc}"))
            col = html.escape(finfo.get("color", "#888888"), quote=True)
            items = flag_groups[fc]
            body.append(
                f"<h2 class='catsec' id='flag-{fc}' style='border-left:6px solid {col}'>"
                f"{lbl} <span class='n'>({len(items):,})</span>"
                f"<a class='toplink' href='#top'>&uarr; top</a></h2>")
            _kind_grids(items)
    else:
        # group by category (ordered by the category's display position, 0
        # last), then within each category by flag (see _section above), with
        # kind (image/video/other) only splitting whatever carries no flag.
        groups: dict[int, dict[str, list[dict]]] = {}
        for d in rows:
            code = d.get("category") or 0
            kind = d.get("kind") if d.get("kind") in ("image", "video") else "other"
            groups.setdefault(code, {}).setdefault(kind, []).append(d)

        def _order(code: int) -> tuple:
            if code == 0:
                return (1, 1e9, 0)
            return (0, cmap.get(code, {}).get("position", code), code)
        codes = sorted(groups, key=_order)

        def _cat_count(code: int) -> int:
            return sum(len(v) for v in groups[code].values())

        if len(codes) > 1:
            toc = "".join(
                f"<a href='#cat-{c}'>{html.escape(categories.label(case.db, c))} "
                f"<span class='n'>{_cat_count(c):,}</span></a>" for c in codes)
            body.append(f"<nav class='toc'><span class='lbl'>Jump to section:</span>{toc}</nav>")
        body.append(rptbar_html)

        # the summary's "By flag" rows link to #flag-{code} whichever mode the
        # report is in; here that flag can recur under several categories, so
        # only the first occurrence (in doc order) gets the anchor
        seen_flag_anchor: set[int] = set()

        for c in codes:
            lbl = html.escape(categories.label(case.db, c))
            col = html.escape(categories.color(case.db, c))
            body.append(
                f"<h2 class='catsec' id='cat-{c}' style='border-left:6px solid {col}'>"
                f"{lbl} <span class='n'>({_cat_count(c):,})</span>"
                f"<a class='toplink' href='#top'>&uarr; top</a></h2>")
            cat_rows = (groups[c].get("image", []) + groups[c].get("video", [])
                        + groups[c].get("other", []))
            flag_rows: dict[int, list[dict]] = {}
            unflagged: list[dict] = []
            for d in cat_rows:
                fl_list = d.get("flags") or []
                if not fl_list:
                    unflagged.append(d)
                for fl in fl_list:
                    flag_rows.setdefault(fl["code"], []).append(d)

            flag_codes = sorted(flag_rows, key=lambda fc: fmap.get(fc, {}).get("position", fc))
            for fc in flag_codes:
                finfo = fmap.get(fc, {})
                anchor = ""
                if fc not in seen_flag_anchor:
                    anchor = f"flag-{fc}"
                    seen_flag_anchor.add(fc)
                _section(html.escape(finfo.get("name", f"Flag {fc}")), flag_rows[fc],
                         swatch=finfo.get("color", "#888888"), anchor=anchor)
            if unflagged:
                _section("No flag", unflagged)

    body.append(_REPORT_JS.replace("__BLUR_DEFAULT__", "true" if blur else "false"))
    body.append("</div></body></html>")
    dest.write_text("".join(body), encoding="utf-8")
    return dest


_REPORT_JS = """
<script>
(function(){
  var root = document.documentElement;
  function get(k){ try{ return localStorage.getItem('gleapp.report.'+k); }catch(e){ return null; } }
  function set(k,v){ try{ localStorage.setItem('gleapp.report.'+k, v); }catch(e){} }
  // cls is applied by default when def is true; a stored '0'/'1' overrides,
  // for toggles that opt into persistence (persist=true).
  function wire(id, cls, key, def, persist){
    var btn = document.getElementById(id);
    if(!btn) return;
    var stored = persist ? get(key) : null;
    var on = stored === null ? def : (stored === '1');
    root.classList.toggle(cls, on);
    btn.classList.toggle('on', on);
    btn.addEventListener('click', function(){
      var v = root.classList.toggle(cls);
      btn.classList.toggle('on', v);
      if(persist) set(key, v ? '1' : '0');
    });
  }
  // Blur starts at whichever default the examiner picked when exporting this
  // report (the Export dialog's "Blur images by default" checkbox) - baked
  // into the file at export time, not persisted across reports, so turning it
  // off to look at one report can't leave it off for the next one they open
  // (local html files share storage per browser).
  wire('btnBlur', 'blur', 'blur', __BLUR_DEFAULT__, false);
  wire('btnDark', 'dark', 'dark', false, true);
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
  // always show every metadata block and image/video group when printing, then restore
  var snap = [];
  window.addEventListener('beforeprint', function(){
    snap = [];
    document.querySelectorAll('details.meta, details.kindsec, details.overview, details.summary').forEach(function(d){
      snap.push(d.open); d.open = true;
    });
  });
  window.addEventListener('afterprint', function(){
    document.querySelectorAll('details.meta, details.kindsec, details.overview, details.summary').forEach(function(d, i){
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
