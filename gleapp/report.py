"""Report + export generation: CSV, JSON, HTML, and KML for geolocation."""

from __future__ import annotations

import csv
import html
import json
import time
from pathlib import Path

from . import categories
from .case import Case

_CSV_FIELDS = [
    "id", "rel_path", "source", "kind", "ext", "size", "created_dt",
    "md5", "sha1", "sha256", "phash", "width", "height", "duration",
    "gps_lat", "gps_lon", "camera", "faces", "skin_ratio",
    "category", "category_label", "triage", "reviewed", "reviewed_by",
    "hashset_hit", "hashset_cat", "stack_id", "vstack_id", "cluster_id", "notes",
    "media_id", "orig_name", "orig_path", "mime",
]


def export_projectvic(case: Case, dest: str | Path, *,
                      only_categorized: bool = False) -> Path:
    """Round-trip export to Project VIC 2.0 JSON (see gleapp.projectvic)."""
    from . import projectvic
    return projectvic.export_vic(case, dest, only_categorized=only_categorized)


def _rows(case: Case, where: str = "") -> list[dict]:
    out = []
    for r in case.db.iter_files(where):
        d = dict(r)
        d["category_label"] = categories.label(case.db, d.get("category") or 0)
        d["tags"] = case.db.tags_for(d["id"])
        out.append(d)
    return out


def _and(*clauses: str) -> str:
    return " AND ".join(f"({c})" for c in clauses if c)


def export_md5(case: Case, dest: str | Path, where: str = "") -> Path:
    """CSV of the distinct MD5 values in scope: header ``md5``, one per line."""
    dest = Path(dest)
    sql = "SELECT DISTINCT md5 FROM files WHERE md5 IS NOT NULL AND md5 != ''"
    if where:
        sql += f" AND ({where})"
    sql += " ORDER BY md5"
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["md5"])
        for (h,) in case.db.conn.execute(sql):
            w.writerow([h])
    return dest


def export_csv(case: Case, dest: str | Path, where: str = "") -> Path:
    dest = Path(dest)
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in _rows(case, where):
            w.writerow(d)
    return dest


def export_json(case: Case, dest: str | Path, where: str = "") -> Path:
    dest = Path(dest)
    payload = {
        "case": case.db.get_meta("case_name"),
        "examiner": case.examiner,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stats": case.db.stats(),
        "files": _rows(case, where),
    }
    dest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return dest


def export_kml(case: Case, dest: str | Path, where: str = "") -> Path:
    """KML of every in-scope file that carries GPS coordinates."""
    dest = Path(dest)
    pts = [d for d in _rows(case, _and("gps_lat IS NOT NULL AND gps_lon IS NOT NULL",
                                       where))]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        f"<name>{html.escape(str(case.db.get_meta('case_name')))} - media geolocation</name>",
    ]
    for d in pts:
        desc = (
            f"{html.escape(d['rel_path'])}<br/>"
            f"{html.escape(str(d.get('created_dt') or ''))}<br/>"
            f"Category: {html.escape(d['category_label'])}"
        )
        parts.append(
            "<Placemark>"
            f"<name>{html.escape(Path(d['rel_path']).name)}</name>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{d['gps_lon']},{d['gps_lat']},0</coordinates></Point>"
            "</Placemark>"
        )
    parts.append("</Document></kml>")
    dest.write_text("\n".join(parts), encoding="utf-8")
    return dest


_HTML_HEAD = """<!doctype html><meta charset="utf-8">
<title>GLEAPP report - {case}</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;color:#1a1a1a;background:#fff}}
 h1{{margin:0 0 .2rem}} .muted{{color:#666}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-top:1rem}}
 .card{{border:1px solid #ddd;border-radius:8px;overflow:hidden;font-size:12px}}
 .card img{{width:100%;height:150px;object-fit:cover;background:#f0f0f0;display:block}}
 .card .b{{padding:6px 8px}}
 .card .catbar{{padding:3px 8px;color:#fff;font-weight:600;font-size:11px}}
 .pill{{display:inline-block;padding:1px 6px;border-radius:10px;background:#eee;margin:1px}}
 table{{border-collapse:collapse;margin-top:1rem}} td,th{{border:1px solid #ddd;padding:4px 8px;text-align:left}}
</style>
"""


def export_html(case: Case, dest: str | Path, where: str = "", *, thumbs: bool = True) -> Path:
    dest = Path(dest)
    rows = _rows(case, where)
    st = case.db.stats()
    cn = html.escape(str(case.db.get_meta("case_name")))

    body = [_HTML_HEAD.format(case=cn)]
    body.append(f"<h1>{cn}</h1>")
    body.append(
        f"<div class='muted'>Examiner: {html.escape(case.examiner)} &middot; "
        f"Generated {time.strftime('%Y-%m-%d %H:%M')}</div>"
    )
    body.append("<table><tr><th>Metric</th><th>Value</th></tr>")
    body.append(f"<tr><td>Total files</td><td>{st['total']}</td></tr>")
    for k, v in st["by_kind"].items():
        body.append(f"<tr><td>&nbsp;&nbsp;{html.escape(str(k))}</td><td>{v}</td></tr>")
    body.append(f"<tr><td>Reviewed</td><td>{st['reviewed']}</td></tr>")
    body.append(f"<tr><td>Known-hash hits</td><td>{st['hashset_hits']}</td></tr>")
    body.append(f"<tr><td>Duplicate stacks</td><td>{st['stacks']}</td></tr>")
    body.append(f"<tr><td>Redundant duplicates</td><td>{st['redundant_duplicates']}</td></tr>")
    body.append(f"<tr><td>Near-dup clusters</td><td>{st['clusters']}</td></tr>")
    for code, n in sorted(st["by_category"].items()):
        body.append(
            f"<tr><td>{html.escape(categories.label(case.db, code))} "
            f"(code {code})</td><td>{n}</td></tr>"
        )
    body.append("</table>")

    body.append("<div class='grid'>")
    thumb_root = case.thumb_dir
    for d in rows:
        code = d.get("category") or 0
        ccol = categories.color(case.db, code)
        catbar = (
            f"<div class='catbar' style='background:{html.escape(ccol)}'>"
            f"{html.escape(d['category_label'])}</div>" if code else ""
        )
        cls = "card"
        img = ""
        if thumbs and d.get("thumb"):
            rel = (thumb_root / d["thumb"]).resolve()
            try:
                rel = rel.relative_to(dest.parent.resolve())
                img = f"<img loading='lazy' src='{html.escape(str(rel).replace(chr(92),'/'))}'>"
            except ValueError:
                img = f"<img loading='lazy' src='file:///{html.escape(str(rel).replace(chr(92),'/'))}'>"
        tags = "".join(f"<span class='pill'>{html.escape(t)}</span>" for t in d["tags"])
        hit = f"<span class='pill'>HASH: {html.escape(d['hashset_hit'])}</span>" if d.get("hashset_hit") else ""
        body.append(
            f"<div class='{cls}'>{img}{catbar}<div class='b'>"
            f"<b>{html.escape(Path(d['rel_path']).name)}</b><br>"
            f"<span class='muted'>{html.escape(d['rel_path'])}</span><br>"
            f"{html.escape(str(d.get('created_dt') or ''))}<br>{tags}{hit}"
            f"</div></div>"
        )
    body.append("</div>")
    dest.write_text("".join(body), encoding="utf-8")
    return dest
