"""Flask review gallery + case-management API.

Runs in two front-ends off the same code:

* ``gleapp web``      - opens in your browser
* ``gleapp desktop``  - opens in a native pywebview window (offline .exe target)

The app can start with **no case open**: ``/api/context`` then reports
``needs_case`` and the UI shows a launcher (recent cases / open / new + ingest).
"""

from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from .. import appconfig, archive, backup, basemaps, categories, flags, lava, report
from ..case import open_case, parse_source_spec
from ..db import ORIGINS, TOOL_ACTOR
from ..facematch import find_matching_faces
from ..pipeline import ingest_sources, process
from ..similar import find_similar

FIELDS = (
    "id, path, rel_path, source, kind, ext, size, mtime, ctime, atime, ingested_at, "
    "created_dt, md5, sha1, sha256, "
    "phash, width, height, duration, gps_lat, gps_lon, camera, faces, "
    "skin_ratio, category, triage, reviewed, reviewed_by, reviewed_at, notes, "
    "hashset_hit, hashset_cat, hashset_kind, hashset_sources, hashset_mask, "
    "stack_id, vstack_id, cluster_id, thumb, error, "
    "media_id, orig_name, orig_path, mime, vic_flags, alt_paths, recorded_times, origin"
)

# columns the details list-view may sort and filter on (must all be in FIELDS)
LIST_COLS = {
    "id", "path", "rel_path", "orig_name", "orig_path", "source", "kind", "ext",
    "mime", "size", "mtime", "ctime", "atime", "created_dt", "ingested_at", "md5", "sha1",
    "sha256", "phash", "width", "height", "duration", "camera", "gps_lat",
    "gps_lon", "faces", "skin_ratio", "category", "triage", "notes",
    "hashset_hit", "hashset_kind", "hashset_cat", "stack_id", "vstack_id",
    "cluster_id", "media_id", "error", "reviewed", "recorded_times", "origin",
}


# Virtual list-view columns that display a fallback chain, so filtering/sorting
# must act on the same COALESCE expression the UI shows (e.g. a folder-ingest
# file has no orig_name - the "Name" column shows its on-disk name instead).
_COL_EXPR = {
    "name":      "COALESCE(NULLIF(orig_name, ''), "
                 "CASE WHEN media_id IS NULL THEN rel_path END)",
    "file_path": "COALESCE(NULLIF(orig_path, ''), "
                 "CASE WHEN media_id IS NULL THEN path END, "
                 "CASE WHEN media_id IS NULL THEN rel_path END)",
}


def _col_sql(col: str) -> str | None:
    """The SQL expression to filter/sort a list-view column on, or None."""
    if col in _COL_EXPR:
        return _COL_EXPR[col]
    return col if col in LIST_COLS else None


def _col_filter_clause(col: str, op: str, val):
    """One (sql, params) pair for a details-list column filter, or None."""
    if col == "flags":
        return ("id IN (SELECT ff.file_id FROM file_flags ff JOIN flags fl "
                "ON fl.code = ff.flag_code WHERE fl.name LIKE ? ESCAPE '\\')",
                [f"%{_like_escape(val)}%"])
    expr = _col_sql(col)
    if expr is None:
        return None
    if op == "contains":
        return (f"{expr} LIKE ? ESCAPE '\\'", [f"%{_like_escape(val)}%"])
    if op == "eq":
        return (f"{expr} = ?", [val])
    if op == "neq":
        return (f"({expr} IS NULL OR {expr} != ?)", [val])
    if op in ("min", "max", "gt", "lt"):
        sym = {"min": ">=", "max": "<=", "gt": ">", "lt": "<"}[op]
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        return (f"CAST({expr} AS REAL) {sym} ?", [num])
    if op == "set":
        return (f"({expr} IS NOT NULL AND {expr} != '')", [])
    if op == "notset":
        return (f"({expr} IS NULL OR {expr} = '')", [])
    return None


def _like_escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _notices_candidates() -> list[Path]:
    """Where NOTICES.txt can be, frozen first, then a source checkout."""
    here = Path(__file__).resolve()
    out = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        out.append(Path(base) / "NOTICES.txt")
    out.append(Path(sys.executable).resolve().parent / "NOTICES.txt")
    out.append(here.parent.parent.parent / "NOTICES.txt")   # repo root
    return out


def create_app(case_dir: str | None = None, *, native: bool = False) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    state: dict = {
        "case": None,
        "native": native,
        "job": {"running": False, "stage": "idle", "done": 0, "total": 0,
                "message": "", "stats": None, "error": None},
        "last_backup": 0.0,
    }
    app.config["STATE"] = state

    if case_dir and (Path(case_dir) / "case.gleapp").exists():
        state["case"] = open_case(case_dir)
        appconfig.push_recent(str(Path(case_dir).resolve()),
                              state["case"].db.get_meta("case_name"))

    # ---- auto-snapshot daemon --------------------------------------
    def _auto_backup_loop() -> None:
        while True:
            time.sleep(60)
            case = state["case"]
            if case is None or not getattr(case.db, "dirty", False):
                continue
            due = time.time() - state["last_backup"] >= backup.AUTO_INTERVAL_MIN * 60
            quiet = time.time() - case.db.last_write >= 20  # let edits settle
            if due and quiet:
                try:
                    snap = backup.snapshot(case, auto=True)
                    case.db.audit_log(TOOL_ACTOR, "snapshot", json.dumps(
                        {"name": snap.name, "size": snap.size, "reason": "auto"}))
                    state["last_backup"] = time.time()
                except Exception:  # noqa: BLE001 - never kill the daemon
                    pass

    threading.Thread(target=_auto_backup_loop, daemon=True).start()

    # ---- helpers ----------------------------------------------------
    def C():
        if state["case"] is None:
            abort(409, description="no case open")
        return state["case"]

    def row_dict(r) -> dict:
        d = dict(r)
        case = state["case"]
        code = d.get("category") or 0
        d["category_label"] = categories.label(case.db, code)
        d["category_color"] = categories.color(case.db, code)
        d["flags"] = [dict(r) for r in case.db.flags_for(d["id"])]
        d["has_keyframes"] = bool(
            case.db.conn.execute(
                "SELECT 1 FROM keyframes WHERE file_id=? LIMIT 1", (d["id"],)
            ).fetchone()
        )
        return d

    # ---- pages ----------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(app.template_folder, "index.html")

    @app.get("/notices")
    def notices():
        """The third-party licence notices this build carries.

        packaging/gleapp.spec writes NOTICES.txt into the bundle, because a licence
        that asks for its notice to travel with a binary copy is not satisfied by a
        file sitting in the source tree. A source checkout has no copy until
        tools/make_notices.py is run, so say so rather than return a bare 404 that
        reads as a missing feature.
        """
        for cand in _notices_candidates():
            if cand.is_file():
                return send_file(cand, mimetype="text/plain")
        return ("This copy of GLEAPP carries no notices file.\n\n"
                "A packaged build has one. From a source checkout, write it with:\n"
                "    python3 tools/make_notices.py\n",
                404, {"Content-Type": "text/plain; charset=utf-8"})

    @app.errorhandler(409)
    def _no_case(e):
        return jsonify({"error": "no_case", "message": str(e.description)}), 409

    @app.errorhandler(Exception)
    def _err(e):
        if isinstance(e, HTTPException):
            if e.code == 409:
                return jsonify({"error": "no_case",
                                "message": str(e.description)}), 409
            return jsonify({"error": e.name, "message": str(e.description)}), e.code
        tb = traceback.format_exc()
        try:
            (appconfig.config_dir() / "last-error.log").write_text(
                tb, encoding="utf-8"
            )
        except OSError:
            pass
        app.logger.error(tb)
        return jsonify({"error": "server_error",
                        "message": f"{type(e).__name__}: {e}",
                        "traceback": tb}), 500

    # ---- launcher / case management ----------------------------
    @app.get("/api/recent")
    def recent():
        return jsonify(appconfig.recent_cases(limit=3))

    @app.post("/api/recent/clear")
    def recent_clear():
        """Forget the recent-cases list. Cases on disk are untouched."""
        appconfig.clear_recent()
        return jsonify({"ok": True})

    @app.post("/api/pick")
    def pick():
        """Native folder/file dialog (desktop mode only).

        Returns {"path": <str|null>, "error": <str|absent>}.  A null path with no
        error means the user cancelled; the UI then falls back to typed input.
        """
        if not state["native"]:
            return jsonify({"path": None, "error": "not running in desktop mode"})
        kind = (request.get_json(silent=True) or {}).get("kind", "folder")
        try:
            import webview
            win = webview.windows[0]
            if kind == "folder":
                res = win.create_file_dialog(webview.FOLDER_DIALOG)
            elif kind == "hashlist":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=(
                        "Hash lists (*.txt;*.csv;*.tsv;*.md5;*.hash;*.lst;*.json)",
                        "All files (*.*)"),
                )
            elif kind == "archive":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=("Extraction or disk image "
                                "(*.zip;*.tar;*.tgz;*.tar.gz;*.tbz2;*.tar.bz2;"
                                "*.txz;*.tar.xz;*.E01;*.e01;*.img;*.dd;*.raw;*.bin;"
                                "*.000;*.001)",
                                "All files (*.*)"),
                )
            elif kind == "hashdb":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=(
                        "Reference data (*.db;*.sqlite;*.sqlite3;*.sql;*.json)",
                        "All files (*.*)"),
                )
            elif kind == "basemap":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=("Basemap (*.pmtiles;*.mbtiles)", "All files (*.*)"),
                )
            elif kind == "stashfile":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=(
                        # .gleapp still accepted so an older exported stash
                        # (from before the .hstash rename) can be merged in
                        "Hash stash (*.hstash;*.gleapp;*.csv)", "All files (*.*)"),
                )
            elif kind == "casefile":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=("GLEAPP case file (*.gleapp)", "All files (*.*)"),
                )
            elif kind == "ingestfile":
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=(
                        "Evidence file (*.zip;*.tar;*.tgz;*.tar.gz;*.tbz2;*.tar.bz2;"
                        "*.txz;*.tar.xz;*.E01;*.e01;*.img;*.dd;*.raw;*.bin;*.000;"
                        "*.001;*.json)",
                        "All files (*.*)"),
                )
            else:
                res = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=("JSON (*.json)", "All files (*.*)"),
                )
            return jsonify({"path": res[0] if res else None})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"path": None, "error": f"{type(exc).__name__}: {exc}"})

    @app.post("/api/open-folder")
    def open_folder():
        """Open a folder in the OS file manager - the reports folder an export
        just wrote to, so clicking that toast takes the examiner straight there."""
        path = (request.get_json(silent=True) or {}).get("path")
        if not path or not Path(path).is_dir():
            return jsonify({"error": "not_found", "message": "That folder is gone"}), 404
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - local path this app itself wrote to
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            return jsonify({"error": "failed", "message": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({"ok": True})

    def _close_current() -> None:
        cur = state["case"]
        if cur is None:
            return
        try:
            if getattr(cur.db, "dirty", False):
                snap = backup.snapshot(cur, auto=True)
                cur.db.audit_log(TOOL_ACTOR, "snapshot", json.dumps(
                    {"name": snap.name, "size": snap.size, "reason": "close"}))
        except Exception:  # noqa: BLE001
            pass
        cur.close()

    state["close_current"] = _close_current

    @app.post("/api/case/close")
    def case_close():
        """Snapshot + close the current case and return to the launcher."""
        if state["job"]["running"]:
            abort(409, description="a job is still running — let it finish first")
        _close_current()
        state["case"] = None
        state["last_backup"] = 0.0
        return jsonify({"ok": True})

    @app.post("/api/case/open")
    def case_open():
        data = request.get_json(force=True)
        path = Path(data["path"])
        if path.is_file():
            path = path.parent   # picked the case.gleapp file itself, not its folder
        if not (path / "case.gleapp").exists():
            abort(404, description=f"no case.gleapp in {path}")
        _close_current()
        state["last_backup"] = 0.0
        state["case"] = open_case(path)
        appconfig.push_recent(str(path.resolve()),
                              state["case"].db.get_meta("case_name"))
        return jsonify({"ok": True, "case": state["case"].db.get_meta("case_name")})

    @app.post("/api/case/create")
    def case_create():
        data = request.get_json(force=True)
        path = Path(data["path"]).resolve()
        examiner = data.get("examiner") or "examiner"

        # A case must never be created inside another case's folder tree - that
        # is how the "reports/ nested case" mix-up happens.  Allow only the
        # target dir itself already being a case (re-create is idempotent).
        for anc in list(path.parents)[:6]:
            if (anc / "case.gleapp").exists():
                abort(400, description=(
                    f"That folder is inside an existing case ({anc}). "
                    "Choose a new, empty folder for the case."))
        if path.name.lower() in {"reports", "thumbs", "views", "backups", "staged",
                                 "cache", "tmp", "extracted"}:
            abort(400, description=(
                f"'{path.name}' is a name GLEAPP uses for a case's own "
                "sub-folders. Pick a different folder name."))

        _close_current()
        state["last_backup"] = 0.0
        state["case"] = open_case(path, create=True, examiner=examiner)
        if data.get("name"):
            state["case"].db.set_meta("case_name", str(data["name"]))
        if data.get("use_stash") is False:
            # default is on (matches an existing case with no meta set yet);
            # only write the flag when the examiner turned it off at creation
            state["case"].db.set_meta("use_stash", "0")
        # Deliberately NOT pushed to "recent" yet - only cases that get files
        # ingested land there (see _run_job), so abandoned shells don't show.
        return jsonify({"ok": True, "case": state["case"].db.get_meta("case_name")})

    @app.post("/api/case/ingest")
    def case_ingest():
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        data = request.get_json(force=True)
        opts = data.get("options", {})

        sources = []
        try:
            if data.get("spec"):
                src, meta = parse_source_spec(data["spec"])
                sources += src
                if meta.get("case"):
                    state["case"].db.set_meta("case_name", str(meta["case"]))
            for s in data.get("sources", []):
                raw = s["path"] if isinstance(s, dict) else s
                if not Path(raw).exists():
                    abort(400, description=f"path does not exist: {raw}")
                src, _ = parse_source_spec(raw)
                if isinstance(s, dict) and s.get("name"):
                    src[0].name = s["name"]
                sources += src
        except (FileNotFoundError, ValueError) as exc:
            abort(400, description=str(exc))
        if not sources:
            abort(400, description="no sources given")
        if opts.get("stage"):
            for s in sources:
                if s.kind == "archive":
                    s.stage = True

        # flip the job to a definite running state *before* returning so the
        # client's first /api/job poll can never race a still-"idle" job
        state["job"] = {"running": True, "stage": "starting", "done": 0,
                        "total": 0, "message": "Starting…", "stats": None,
                        "error": None}
        t = threading.Thread(
            target=_run_job, args=(state, sources, opts), daemon=True
        )
        t.start()
        return jsonify({"ok": True, "sources": [s.name for s in sources]})

    @app.get("/api/job")
    def job():
        return jsonify(state["job"])

    @app.post("/api/screen")
    def run_screening():
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        case = state["case"]
        state["job"] = {"running": True, "stage": "process", "done": 0,
                        "total": 0, "message": "Face / skin screening…",
                        "stats": None, "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                from ..pipeline import screen_pass
                n = screen_pass(case, workers=4,
                                progress=lambda d, t: j.update(done=d, total=t))
                j.update(running=False, stage="done", message="Screening done",
                         stats={"screened": n})
            except Exception as exc:  # noqa: BLE001
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/reprocess-errors")
    def reprocess_errors():
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        case = state["case"]
        n = case.db.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE error IS NOT NULL").fetchone()["n"]
        state["job"] = {"running": True, "stage": "process", "done": 0,
                        "total": n, "message": f"Retrying {n} failed files…",
                        "stats": None, "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                from ..pipeline import process
                st = process(case, where="error IS NOT NULL", force=True,
                             screen=False, reason="retry-errors",
                             progress=lambda d, t: j.update(done=d, total=t),
                             stage_cb=lambda m: j.update(message=m))
                fixed = n - case.db.conn.execute(
                    "SELECT COUNT(*) n FROM files WHERE error IS NOT NULL"
                ).fetchone()["n"]
                j.update(running=False, stage="done",
                         message=f"Recovered {fixed} of {n}",
                         stats={**st.as_dict(), "recovered": fixed})
            except Exception as exc:  # noqa: BLE001
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True, "count": n})

    @app.post("/api/expand-archives")
    def expand_archives():
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        case = state["case"]
        force = bool((request.get_json(silent=True) or {}).get("force"))
        n = case.db.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE kind = 'archive' OR "
            "(kind = 'other' AND lower(ext) IN "
            "('.zip','.tar','.gz','.tgz','.bz2','.tbz2','.xz','.txz','.7z','.rar'))"
        ).fetchone()["n"]
        state["job"] = {"running": True, "stage": "process", "done": 0, "total": 0,
                        "message": f"Opening {n} archive(s)…", "stats": None,
                        "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                from .. import nested, winsearch
                added = nested.expand_containers(
                    case, force=force,
                    progress=lambda k: j.update(done=k, message=f"{k:,} files found"))
                try:
                    winsearch.correlate_thumbnails(case)
                except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                    pass
                if added:
                    j.update(stage="process", done=0, total=added,
                             message=f"Processing {added:,} extracted file(s)…")
                    process(case, where="md5 IS NULL", reason="expand-archives",
                            progress=lambda d, t: j.update(done=d, total=t),
                            stage_cb=lambda m: j.update(message=m))
                j.update(running=False, stage="done",
                         message=(f"{added:,} file(s) recovered from archives"
                                  if added else "No new files in the archives"),
                         stats={"expanded": added})
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True, "count": n})

    @app.post("/api/rehash")
    def rehash():
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        _rematch_job(state["case"], "Re-checking known hashes…")
        return jsonify({"ok": True})

    @app.get("/api/hashsets")
    def hashsets_list():
        from .. import hashstore
        return jsonify(hashstore.summary())

    def _rematch_job(case, msg: str) -> None:
        """Spawn the background 'flag files against every loaded hash set' pass."""
        state["job"] = {"running": True, "stage": "process", "done": 0,
                        "total": 0, "message": msg, "stats": None, "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                from ..pipeline import rematch_hashes
                hits = rematch_hashes(
                    case, progress=lambda d, t: j.update(done=d, total=t))
                j.update(running=False, stage="done",
                         message=f"{hits} known-hash hit(s)",
                         stats={"hashset_hits": hits})
            except Exception as exc:  # noqa: BLE001
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()

    @app.get("/api/hashsets/case")
    def hashsets_case():
        case = C()
        return jsonify([dict(r) for r in case.db.list_hashsets()])

    @app.post("/api/hashset/import")
    def hashset_import():
        """Import a hash list (CyberTip MD5s, CAID, CSV, VIC JSON, ...) into
        this case and re-flag every file against it."""
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        case = state["case"]
        data = request.get_json(force=True) or {}
        raw = str(data.get("path", "")).strip().strip('"')
        if not raw or not Path(raw).is_file():
            abort(400, description=f"file not found: {raw or '(none)'}")
        kind = data.get("kind") if data.get("kind") in (
            "known", "known-good", "other") else "known"
        name = str(data.get("name", "")).strip() or Path(raw).stem
        from .. import hashdb
        refusal = hashdb.kind_refusal(raw, kind)
        if refusal:
            abort(400, description=refusal)
        try:
            hs_id, added = hashdb.import_hashset(
                case.db, raw, name=name, kind=kind, actor=case.examiner)
            counts = hashdb.algo_counts(case.db.conn, hs_id)
        except (OSError, ValueError, sqlite3.Error) as exc:
            abort(400, description=f"could not read hash list: {exc}")
        note = hashdb.photodna_note(counts)
        case.db.audit_log(case.examiner, "hashset_import", json.dumps({
            "name": name, "kind": kind, "added": added,
            "source": Path(raw).name,  # basename only - never the full local path
            "photodna": counts.get(hashdb.PHOTODNA_ALGO, 0),
        }))
        _rematch_job(case, f"Flagging files against {name}…")
        return jsonify({"ok": True, "id": hs_id, "name": name,
                        "kind": kind, "entries": added,
                        "photodna": counts.get(hashdb.PHOTODNA_ALGO, 0),
                        "photodna_note": note})

    @app.post("/api/hashset/remove")
    def hashset_remove():
        if state["case"] is None:
            abort(409, description="no case open")
        case = state["case"]
        hs_id = int((request.get_json(force=True) or {}).get("id", 0))
        row = case.db.conn.execute(
            "SELECT name, kind FROM hashsets WHERE id=?", (hs_id,)).fetchone()
        if row is None:
            abort(404, description="no such hash set")
        affected = case.db.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE hashset_hit=?", (row["name"],)
        ).fetchone()["n"]
        # the delete + flag-clear is instant, so it's fine even mid-job
        removed = case.db.delete_hashset(hs_id)
        case.db.audit_log(case.examiner, "hashset_remove", json.dumps(
            {"name": removed, "kind": row["kind"], "files_unflagged": affected}))
        # re-evaluate the now-unflagged files against the *remaining* sets so an
        # overlap (e.g. NSRL) re-flags them - unless a job is already running
        rematched = False
        if not state["job"]["running"]:
            _rematch_job(case, f"Updating flags after removing {removed}…")
            rematched = True
        return jsonify({"ok": True, "name": removed, "rematched": rematched})

    # ---- global reference store (NSRL RDS and other large sets) -------
    @app.post("/api/hashset/global/import")
    def hashset_global_import():
        """Import a reference set into the shared global store, in the
        background: an NSRL RDS ``.db``, a ``.sql`` dump, a quarterly
        ``_delta.sql`` merged onto the previous full ``.db`` (``base``), or a
        Project VIC hash set (``.json``), which is read as it is imported."""
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        from .. import hashdb, hashstore
        data = request.get_json(force=True) or {}
        src = str(data.get("path", "")).strip().strip('"')
        base = str(data.get("base", "")).strip().strip('"')
        schema = str(data.get("schema", "")).strip().strip('"')
        kind = data.get("kind") if data.get("kind") in (
            "known", "known-good", "other") else "known-good"
        algos = tuple(a for a in (data.get("algos") or ["md5"])
                      if a in ("md5", "sha1", "sha256")) or ("md5",)
        if not src or not Path(src).is_file():
            abort(400, description=f"file not found: {src or '(none)'}")
        if data.get("vic"):
            # the Project VIC dialog: it takes a Project VIC hash set and nothing
            # else, and a Project VIC set is always notable, never benign
            if not hashdb.is_vics_file(src):
                abort(400, description=(
                    f"{Path(src).name} is not a Project VIC hash set (a JSON list "
                    "of Media records carrying an MD5 and a MediaID). Use "
                    "Reference data for an NSRL or other set."))
            kind = "known"
        is_delta = bool(base) or src.lower().endswith("_delta.sql")
        if is_delta and not (base and Path(base).is_file()):
            abort(400, description="a quarterly delta needs the previous full "
                  ".db in the 'base' field")
        if schema and not Path(schema).is_file():
            abort(400, description=f"schema file not found: {schema}")
        name = str(data.get("name", "")).strip() or Path(src).stem
        if not (schema or is_delta):
            # checked here, before the job starts, so the examiner sees why
            refusal = hashdb.kind_refusal(src, kind)
            if refusal:
                abort(400, description=refusal)
        case = state["case"]
        state["job"] = {"running": True, "stage": "process", "done": 0,
                        "total": 0, "stats": None, "error": None,
                        "message": f"Importing {name} into the reference store…"}

        def _job() -> None:
            j = state["job"]
            try:
                path = src
                if schema:
                    j.update(message=f"Building a database from {Path(src).name}…")
                    path = str(hashstore.build_db(
                        schema, src, out_db=Path(src).with_suffix(".db")))
                elif is_delta:
                    j.update(message=f"Merging {Path(src).name} onto "
                             f"{Path(base).name}…")
                    path = str(hashstore.apply_delta(base, src))
                _hs, added = hashstore.import_path(
                    path, name=name, kind=kind, algos=algos,
                    progress=lambda seen, add: j.update(
                        done=add, message=f"{name}: {add:,} hashes stored "
                        f"({seen:,} rows scanned)"))
                hits = 0
                if case is not None:
                    from ..pipeline import rematch_hashes
                    hits = rematch_hashes(
                        case, progress=lambda d, t: j.update(
                            done=d, total=t,
                            message="Re-checking case files against known hashes…"))
                note = hashdb.photodna_note(hashstore.algo_counts(_hs))
                j.update(running=False, stage="done",
                         stats={"entries": added, "hashset_hits": hits,
                                "photodna_note": note},
                         message=f"{name}: {added:,} hashes imported"
                         + (f" — {hits} case hit(s)" if case is not None else "")
                         + (f". {note}" if note else ""))
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True, "name": name})

    @app.post("/api/hashset/global/remove")
    def hashset_global_remove():
        from .. import hashstore
        hs_id = int((request.get_json(force=True) or {}).get("id", 0))
        hashstore.delete_set(hs_id)
        rematched = False
        if state["case"] is not None and not state["job"]["running"]:
            _rematch_job(state["case"],
                         "Updating flags after removing a reference set…")
            rematched = True
        return jsonify({"ok": True, "rematched": rematched})

    # ---- local hash stash (its own file - see gleapp/stash.py) -------
    def _stash_candidates(case):
        """This case's files eligible for the stash: (md5, category) in 1-3."""
        from .. import stash
        ph = ",".join("?" * len(stash.STASH_CATEGORIES))
        return case.db.conn.execute(
            f"SELECT md5, category FROM files WHERE category IN ({ph}) "
            "AND md5 IS NOT NULL AND md5 != ''",
            tuple(stash.STASH_CATEGORIES)).fetchall()

    @app.get("/api/stash")
    def stash_status():
        # the stash itself is global, not case data, so it's also reachable
        # pre-case (from the launcher) with no case's hashes to add yet
        from .. import stash
        case = state["case"]
        eligible, by_cat = 0, {}
        if case is not None:
            cand = _stash_candidates(case)
            eligible = len(cand)
            for r in cand:
                by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
        return jsonify({
            "stash": stash.summary(),
            "case": {"eligible": eligible, "by_category": by_cat,
                     "categories": list(stash.STASH_CATEGORIES)},
        })

    @app.post("/api/stash/add")
    def stash_add():
        from .. import stash
        case = C()
        cand = _stash_candidates(case)
        label = case.db.get_meta("case_name") or case.root.name
        res = stash.add((r["md5"], r["category"], label) for r in cand)
        case.db.audit_log(case.examiner, "stash_add",
                          f"{res['submitted']} md5s (cat {stash.STASH_CATEGORIES}), "
                          f"{res['added']} new; stash now {res['total']}")
        return jsonify({"ok": True, **res})

    @app.post("/api/stash/clear")
    def stash_clear():
        from .. import stash
        n = stash.clear()
        # the stash is global and reachable pre-case (from the launcher); only
        # log to a case's own audit trail when there's a case open to log to
        case = state["case"]
        if case is not None:
            case.db.audit_log(case.examiner, "stash_clear", f"{n} entries removed")
        return jsonify({"ok": True, "removed": n})

    @app.post("/api/stash/path")
    def stash_set_path():
        from .. import stash
        p = str((request.get_json(force=True) or {}).get("path", "")).strip()
        try:
            new = stash.set_path(p or None)
        except OSError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, **stash.summary(), "path": str(new)})

    @app.post("/api/stash/export")
    def stash_export():
        from .. import stash
        body = request.get_json(silent=True) or {}
        fmt = "csv" if body.get("format") == "csv" else "db"
        out_dir = appconfig.data_dir() / "hashsets"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = out_dir / (f"hash-stash-{stamp}." + ("csv" if fmt == "csv" else "hstash"))
        try:
            stash.export(dest)
        except OSError as exc:
            abort(500, description=str(exc))
        return jsonify({"ok": True, **stash.summary(),
                        "written": str(dest), "dir": str(out_dir)})

    @app.post("/api/stash/merge")
    def stash_merge():
        from .. import stash
        src = str((request.get_json(force=True) or {}).get("path", "")).strip()
        if not src or not Path(src).is_file():
            abort(400, description=f"file not found: {src}")
        try:
            res = stash.merge(src)
        except (OSError, ValueError, sqlite3.Error) as exc:
            abort(400, description=f"could not read {Path(src).name}: {exc}")
        # the stash is global and reachable pre-case (from the launcher); only
        # log to a case's own audit trail when there's a case open to log to
        case = state["case"]
        if case is not None:
            case.db.audit_log(case.examiner, "stash_merge",
                              f"{Path(src).name}: +{res['added']} new, {res['total']} total")
        return jsonify({"ok": True, **res})

    @app.post("/api/redup")
    def run_redup():
        if state["case"] is None:
            abort(409, description="no case open")
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        case = state["case"]
        state["job"] = {"running": True, "stage": "process", "done": 0,
                        "total": 1, "message": "Re-scanning for duplicates…",
                        "stats": None, "error": None}

        def _job() -> None:
            j = state["job"]

            def progress(d, t):
                j.update(done=d, total=t)

            try:
                from .. import dedupe
                j.update(message="Stacking exact duplicates…", done=0, total=0)
                red = dedupe.stack_exact(case.db, progress=progress)
                j.update(message="Stacking visual matches…", done=0, total=0)
                vs = dedupe.stack_visual(case.db, progress=progress)
                j.update(message="Clustering near-duplicates…", done=0, total=0)
                cl = dedupe.cluster_near(case.db, threshold=12, progress=progress)
                case.db.audit_log(case.examiner, "redup", json.dumps(
                    {"redundant_duplicates": red, "visual_stacks": vs, "clusters": cl}))
                j.update(running=False, stage="done", message="Done",
                         stats={"redundant_duplicates": red, "visual_stacks": vs,
                                "clusters": cl})
            except Exception as exc:  # noqa: BLE001
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True})

    # ---- static media ------------------------------------------
    @app.get("/thumb/<path:name>")
    def thumb(name: str):
        return send_from_directory(C().thumb_dir, name, max_age=3600)

    def _local(r) -> Path:
        """The file holding a row's bytes: its own path, or, for a row whose bytes
        live in an extraction zip, a copy kept under <case>/cache/. 410 with the
        reason when neither can be produced."""
        case = C()
        p = Path(r["path"])
        if p.exists():
            return p
        rec = archive.source_record(case, r["source"]) if r["source"] else None
        if rec is None:
            abort(410, description="the original file is not on disk")
        try:
            return archive.cached_copy(case.root, rec, r)
        except archive.ArchiveUnavailable as exc:
            abort(410, description=str(exc))

    def _display_path(r, local: Path) -> Path:
        """The file to actually show: the media unpacked from an LZC bundle
        during processing, or the file itself."""
        ex_dir = C().root / "extracted"
        if ex_dir.is_dir():
            hit = next(ex_dir.glob(f"{r['id']}.*"), None)
            if hit:
                return hit
        return local

    @app.get("/media/<int:file_id>")
    def media(file_id: int):
        r = C().db.get_file(file_id)
        if not r:
            abort(404)
        p = _display_path(r, _local(r))
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return send_file(p, mimetype=mime, conditional=True,
                         download_name=r["orig_name"] or p.name)

    @app.get("/view/<int:file_id>")
    def view(file_id: int):
        """Full-size display image. Serves the raw file when the browser can
        render it; otherwise decodes (HEIC/TIFF/RAW/…) to a cached JPEG."""
        from .. import imaging
        case = C()
        r = case.db.get_file(file_id)
        if not r:
            abort(404)
        p = _display_path(r, _local(r))
        # Apple CgBI ("iPhone-optimised") PNGs carry a .png name but no browser
        # can render them - transcode to a real PNG, keeping transparency.
        cgbi = p.suffix.lower() == ".png" and imaging.is_cgbi_png(p)
        if cgbi:
            cache = case.root / "views" / f"{file_id}.png"
            if not cache.exists() and not imaging.to_web_png(p, cache):
                abort(415, description="Apple CgBI PNG could not be decoded")
            return send_file(cache, mimetype="image/png", conditional=True)
        if r["kind"] == "video" or p.suffix.lower() in imaging.WEB_IMAGE_EXTS:
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            return send_file(p, mimetype=mime, conditional=True)
        cache = case.root / "views" / f"{file_id}.jpg"
        if not cache.exists() and not imaging.transcode_isolated(p, cache):
            abort(415, description=f"{p.suffix or 'this file'} can't be displayed "
                  "(proprietary Apple asset / unknown codec)")
        return send_file(cache, mimetype="image/jpeg", conditional=True)

    @app.get("/api/file/<int:file_id>/keyframes")
    def keyframes(file_id: int):
        return jsonify([
            {"ts": k["ts"], "thumb": f"/thumb/{k['thumb']}"}
            for k in C().db.keyframes_for(file_id)
        ])

    @app.get("/api/file/<int:file_id>/hex")
    def file_hex(file_id: int):
        """A slice of the raw bytes of a file, hex-encoded, for the hex viewer."""
        r = C().db.get_file(file_id)
        if not r:
            abort(404)
        p = _local(r)
        try:
            size = p.stat().st_size
            offset = max(0, int(request.args.get("offset", 0)))
            length = min(max(16, int(request.args.get("length", 2048))), 65536)
            with open(p, "rb") as fh:
                fh.seek(min(offset, size))
                data = fh.read(length)
        except OSError as exc:
            abort(500, description=str(exc))
        return jsonify({"name": r["orig_name"] or p.name, "size": size,
                        "offset": min(offset, size), "bytes": data.hex()})

    # ---- listing / filtering --------------------------------
    @app.get("/api/files")
    def list_files():
        case = C()
        q = request.args
        where, params = [], []

        def eq(col: str, val):
            where.append(f"{col} = ?")
            params.append(val)

        # Browsing one exact/visual-stack group (stack= / vstack=) is an explicit
        # request to see every member of it. Any other active filter - a leftover
        # search box, a category, "Has faces", ... - must not hide a sibling that
        # doesn't happen to match it too (the usual way an examiner reaches a group
        # is by searching for or filtering down to the one file whose badge they
        # clicked), so a group request skips every other clause below entirely,
        # not just collapse.
        group = q.get("stack") or q.get("vstack")
        if group:
            eq("stack_id" if q.get("stack") else "vstack_id", int(group))
        else:
            if q.get("kind"):
                eq("kind", q["kind"])
            else:
                # An archive container (a .zip / .tar found in a source) is kept
                # in the case for its hashes and to link its members, but it is
                # not media - it stays out of the gallery unless asked for by name
                # (Type = "archive (container)").
                where.append("kind != 'archive'")
            if q.get("source"):
                eq("source", q["source"])
            # Every kind the ingest writes, read from one vocabulary: an origin
            # missing from this tuple is not rejected, it is ignored, so the
            # filter silently returns the whole case. "deleted" was missing.
            if q.get("origin") in ORIGINS:
                eq("origin", q["origin"])
            if q.get("in_archive") == "1":
                where.append("container_id IS NOT NULL")
            if q.get("category") not in (None, "", "any"):
                eq("category", int(q["category"]))
            if q.get("flag") not in (None, "", "any"):
                where.append("id IN (SELECT file_id FROM file_flags WHERE flag_code=?)")
                params.append(int(q["flag"]))
            if q.get("cluster"):
                eq("cluster_id", int(q["cluster"]))
            if q.get("hashset") == "1":
                where.append("hashset_hit IS NOT NULL")
            _hn = q.get("hashset_name", "")
            if _hn == "*":                       # any set imported into this case
                where.append("hashset_hit IN (SELECT name FROM hashsets)")
            elif _hn == "*good":                 # any NSRL / known-good reference hit
                where.append("hashset_kind = 'known-good'")
            elif _hn in ("*vic", "*stash", "*both"):
                # which kinds of source flagged the file (files.hashset_mask), so
                # a file that hit both a Project VIC set and the stash is in each
                from ..db import MATCH_STASH, MATCH_VIC
                bits = {"*vic": MATCH_VIC, "*stash": MATCH_STASH,
                        "*both": MATCH_VIC | MATCH_STASH}[_hn]
                where.append("(hashset_mask & ?) = ?")
                params.extend([bits, bits])
            elif _hn:                            # one named set (or the hash stash)
                from ..db import source_like
                where.append("(hashset_hit = ? OR hashset_sources LIKE ? ESCAPE '!')")
                params.extend([_hn, "%" + source_like(_hn) + "%"])
            if q.get("hidegood") == "1":
                where.append("(hashset_kind IS NULL OR hashset_kind != 'known-good')")
            if q.get("faces") == "1":
                where.append("faces > 0")
            # "has duplicates": a real >=2 exact stack, a visual stack, or a
            # near-dup cluster (vstack_id/cluster_id are only set for groups of >=2)
            _exact_dup = ("stack_id IN (SELECT stack_id FROM files WHERE stack_id "
                          "IS NOT NULL GROUP BY stack_id HAVING COUNT(*) > 1)")
            _dup = {
                "exact": _exact_dup,
                "visual": "vstack_id IS NOT NULL",
                "cluster": "cluster_id IS NOT NULL",
                "any": f"(vstack_id IS NOT NULL OR cluster_id IS NOT NULL OR {_exact_dup})",
            }.get(q.get("hasdup"))
            if _dup:
                where.append(_dup)
            if q.get("min_skin"):
                where.append("skin_ratio >= ?")
                params.append(float(q["min_skin"]))
            if q.get("has_gps") == "1":
                where.append("gps_lat IS NOT NULL")
            if q.get("error") == "1":
                where.append("error IS NOT NULL")
            elif q.get("error") == "0":
                where.append("error IS NULL")
            if q.get("q", "").strip():
                # every whitespace-separated term must match somewhere (AND);
                # within a term, match across every text/metadata column (OR).
                # Name/path resolve to the *device* name and path for a Project VIC
                # file (MediaFiles.FileName / .FilePath); rel_path is only searched
                # for a plain folder ingest (media_id IS NULL). Otherwise the local
                # extraction folder the VIC files were unpacked into - which is on
                # every row - would match every search.
                _rp = "CASE WHEN media_id IS NULL THEN rel_path END"
                cols = (f"COALESCE(NULLIF(orig_name, ''), {_rp})",
                        f"COALESCE(NULLIF(orig_path, ''), {_rp})",
                        "alt_paths",            # the other storage views a file sat under
                        "camera", "notes", "mime", "source", "created_dt",
                        "reviewed_by", "hashset_hit", "error",
                        "md5", "sha1", "sha256", "phash")
                for word in q["q"].split():
                    term = f"%{word}%"
                    clause = " OR ".join(f"{c} LIKE ?" for c in cols)
                    clause += (" OR id IN (SELECT ff.file_id FROM file_flags ff "
                               "JOIN flags fl ON fl.code = ff.flag_code "
                               "WHERE fl.name LIKE ?)")
                    where.append(f"({clause})")
                    params += [term] * (len(cols) + 1)
            # details list-view: per-column filters (JSON: [{col,op,val}, …])
            if q.get("colfilters"):
                try:
                    for f in json.loads(q["colfilters"]):
                        got = _col_filter_clause(f.get("col"), f.get("op"), f.get("val"))
                        if got:
                            where.append(got[0])
                            params += got[1]
                except (ValueError, TypeError):
                    pass

        # Browsing one exact/visual-stack group (above) is exactly a request to see
        # every member of it - collapsing to one representative row would hide the
        # very copies the examiner asked for.
        collapse = q.get("dupes") == "collapse" and not group

        _legacy_sort = {
            "path": "rel_path", "date": "created_dt", "size": "size",
            "skin": "skin_ratio DESC", "faces": "faces DESC",
            "cluster": "cluster_id", "id": "id",
        }
        _sort_expr = _col_sql(q.get("sort", ""))
        if _sort_expr is not None:
            _dir = "DESC" if q.get("dir", "asc").lower() == "desc" else "ASC"
            sort = f"{_sort_expr} {_dir}, id {_dir}"
        else:
            sort = _legacy_sort.get(q.get("sort", "path"), "rel_path")

        limit = min(int(q.get("limit", 500)), 5000)
        offset = int(q.get("offset", 0))
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        if collapse:
            # One row per visual group (exact stack / visual stack), and the
            # representative is picked from the rows that already match the
            # filters - so an attribute filter (Has GPS, faces, camera, ...)
            # still surfaces a group when only a non-head member carries it.
            grp = "COALESCE(vstack_id, stack_id, id)"
            sql = (f"SELECT {FIELDS} FROM (SELECT {FIELDS}, ROW_NUMBER() OVER "
                   f"(PARTITION BY {grp} ORDER BY id) AS _rn "
                   f"FROM files{where_sql}) g WHERE g._rn = 1 "
                   f"ORDER BY {sort} LIMIT ? OFFSET ?")
            rows = case.db.conn.execute(sql, (*params, limit, offset)).fetchall()
            total = case.db.conn.execute(
                f"SELECT COUNT(DISTINCT {grp}) n FROM files{where_sql}",
                tuple(params)).fetchone()["n"]
        else:
            sql = (f"SELECT {FIELDS} FROM files{where_sql} "
                   f"ORDER BY {sort} LIMIT ? OFFSET ?")
            rows = case.db.conn.execute(sql, (*params, limit, offset)).fetchall()
            total = case.db.conn.execute(
                f"SELECT COUNT(*) n FROM files{where_sql}",
                tuple(params)).fetchone()["n"]

        # stack / visual-stack sizes for the ids on this page (two aggregate queries)
        def _counts(col: str, ids: set) -> dict:
            if not ids:
                return {}
            ph = ",".join("?" * len(ids))
            return {row[col]: row["n"] for row in case.db.conn.execute(
                f"SELECT {col}, COUNT(*) n FROM files "
                f"WHERE {col} IN ({ph}) GROUP BY {col}", tuple(ids))}

        stack_n = _counts("stack_id", {r["stack_id"] for r in rows if r["stack_id"]})
        vstack_n = _counts("vstack_id", {r["vstack_id"] for r in rows if r["vstack_id"]})

        out = []
        for r in rows:
            d = row_dict(r)
            d["stack_count"] = stack_n.get(r["stack_id"], 1)
            d["vstack_count"] = vstack_n.get(r["vstack_id"], 0)
            out.append(d)
        return jsonify({"total": total, "offset": offset, "files": out})

    @app.get("/api/file/<int:file_id>")
    def get_file(file_id: int):
        case = C()
        r = case.db.get_file(file_id)
        if not r:
            abort(404)
        d = row_dict(r)
        d["keyframes"] = [
            {"id": k["id"], "ts": k["ts"], "thumb": f"/thumb/{k['thumb']}"}
            for k in case.db.keyframes_for(file_id)
        ]
        if r["stack_id"]:
            d["stack"] = [row_dict(x) for x in
                          case.db.iter_files("stack_id = ?", (r["stack_id"],))]
        vsid = r["vstack_id"]
        if vsid:
            d["vstack"] = [dict(x) for x in case.db.conn.execute(
                f"SELECT {FIELDS} FROM files WHERE vstack_id=? ORDER BY id", (vsid,))]
        if r["cluster_id"]:
            d["cluster_size"] = case.db.conn.execute(
                "SELECT COUNT(*) n FROM files WHERE cluster_id=?", (r["cluster_id"],)
            ).fetchone()["n"]
        return jsonify(d)

    @app.get("/api/similar/<int:file_id>")
    def similar(file_id: int):
        thr = int(request.args.get("threshold", 12))
        case = C()
        hits = find_similar(case, file_id, threshold=thr, limit=300)
        for h in hits:
            code = h.get("category") or 0
            h["category_label"] = categories.label(case.db, code)
            h["category_color"] = categories.color(case.db, code)
        return jsonify({"file_id": file_id, "count": len(hits), "files": hits})

    @app.get("/api/faces/<int:file_id>")
    def faces_for_file(file_id: int):
        """Detected faces for one file, for drawing clickable box overlays.
        No embedding bytes go over the wire - just enough to draw a box and,
        for one with an embedding, offer "Find matching faces"."""
        case = C()
        return jsonify([
            {"id": f["id"], "bbox": [f["x"], f["y"], f["w"], f["h"]],
             "score": f["score"], "has_embedding": f["embedding"] is not None,
             # null for an image's own face; for a video, which key frame
             # (see /api/file/<id> "keyframes") the box belongs to
             "keyframe_id": f["keyframe_id"]}
            for f in case.db.faces_for(file_id)
        ])

    @app.get("/api/face-match/<int:face_id>")
    def face_match(face_id: int):
        case = C()
        hits = find_matching_faces(case, face_id, limit=300)
        for h in hits:
            code = h.get("category") or 0
            h["category_label"] = categories.label(case.db, code)
            h["category_color"] = categories.color(case.db, code)
        return jsonify({"face_id": face_id, "count": len(hits), "files": hits})

    # ---- categories -----------------------------------------
    @app.get("/api/categories")
    def categories_list():
        case = C()
        return jsonify(list(categories.catmap(case.db).values()))

    @app.post("/api/categories")
    def categories_add():
        case = C()
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", ""))
        code = case.db.add_category(name, notable=bool(data.get("notable", True)))
        case.db.audit_log(case.examiner, "category_add",
                          json.dumps({"code": code, "name": name}))
        return jsonify(categories.catmap(case.db)[code])

    @app.patch("/api/categories/<int:code>")
    def categories_update(code: int):
        case = C()
        data = request.get_json(force=True)
        fields = {k: data[k] for k in ("name", "color", "notable", "active")
                  if k in data}
        if "notable" in fields:
            fields["notable"] = 1 if fields["notable"] else 0
        if "active" in fields:
            fields["active"] = 1 if fields["active"] else 0
        try:
            case.db.update_category(code, **fields)
        except ValueError as exc:
            abort(400, description=str(exc))
        case.db.audit_log(case.examiner, "category_update", json.dumps(
            {"code": code, "name": categories.label(case.db, code), "changed": fields}))
        return jsonify(categories.catmap(case.db).get(code, {}))

    @app.delete("/api/categories/<int:code>")
    def categories_delete(code: int):
        case = C()
        reassign = request.args.get("reassign") == "1"
        name = categories.label(case.db, code)  # before it's gone
        try:
            case.db.delete_category(code, reassign=reassign)
        except ValueError as exc:
            abort(400, description=str(exc))
        case.db.audit_log(case.examiner, "category_delete", json.dumps(
            {"code": code, "name": name, "reassigned_to_uncategorized": reassign}))
        return jsonify({"ok": True})

    @app.post("/api/categories/reorder")
    def categories_reorder():
        case = C()
        data = request.get_json(force=True)
        codes = [int(c) for c in data["codes"]]
        case.db.reorder_categories(codes)
        order = [categories.label(case.db, c) for c in codes]
        case.db.audit_log(case.examiner, "category_reorder", json.dumps({"order": order}))
        return jsonify(list(categories.catmap(case.db).values()))

    # ---- flags ------------------------------------------------
    @app.get("/api/flags")
    def flags_list():
        case = C()
        return jsonify(list(flags.flagmap(case.db).values()))

    @app.post("/api/flags")
    def flags_add():
        case = C()
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", ""))
        code = case.db.add_flag(name)
        case.db.audit_log(case.examiner, "flag_add",
                          json.dumps({"code": code, "name": name}))
        return jsonify(flags.flagmap(case.db)[code])

    @app.patch("/api/flags/<int:code>")
    def flags_update(code: int):
        case = C()
        data = request.get_json(force=True)
        fields = {k: data[k] for k in ("name", "color") if k in data}
        case.db.update_flag(code, **fields)
        case.db.audit_log(case.examiner, "flag_update", json.dumps(
            {"code": code, "changed": fields}))
        return jsonify(flags.flagmap(case.db).get(code, {}))

    @app.delete("/api/flags/<int:code>")
    def flags_delete(code: int):
        case = C()
        row = case.db.get_flag(code)
        name = row["name"] if row else ""
        case.db.delete_flag(code)
        case.db.audit_log(case.examiner, "flag_delete", json.dumps(
            {"code": code, "name": name}))
        return jsonify({"ok": True})

    @app.post("/api/flags/reorder")
    def flags_reorder():
        case = C()
        data = request.get_json(force=True)
        codes = [int(c) for c in data["codes"]]
        case.db.reorder_flags(codes)
        case.db.audit_log(case.examiner, "flag_reorder", json.dumps({"order": codes}))
        return jsonify(list(flags.flagmap(case.db).values()))

    # ---- mutations ------------------------------------------
    @app.post("/api/categorize")
    def categorize():
        case = C()
        data = request.get_json(force=True)
        cat = int(data["category"])
        ids = [int(fid) for fid in data["ids"]]
        for fid in ids:
            case.db.update_file(fid, category=cat)
        case.db.audit_log(case.examiner, "categorize", json.dumps(
            {"category": cat, "label": categories.label(case.db, cat),
             "count": len(ids), "ids": ids}))
        case.db.commit()
        return jsonify({"ok": True, "category": cat,
                        "label": categories.label(case.db, cat),
                        "color": categories.color(case.db, cat)})

    @app.post("/api/review")
    def review():
        case = C()
        data = request.get_json(force=True)
        val = 1 if data.get("reviewed", True) else 0
        for fid in data["ids"]:
            case.db.update_file(int(fid), reviewed=val,
                                reviewed_at=time.time() if val else None,
                                reviewed_by=case.examiner if val else None)
        case.db.commit()
        return jsonify({"ok": True, "reviewed": val})

    @app.post("/api/triage")
    def triage():
        case = C()
        data = request.get_json(force=True)
        for fid in data["ids"]:
            case.db.update_file(int(fid), triage=data.get("triage"))
        case.db.commit()
        return jsonify({"ok": True})

    @app.post("/api/flag")
    def flag():
        case = C()
        data = request.get_json(force=True)
        for fid in data["ids"]:
            for code in data.get("add", []):
                case.db.add_file_flag(int(fid), int(code))
            for code in data.get("remove", []):
                case.db.remove_file_flag(int(fid), int(code))
        case.db.commit()
        return jsonify({"ok": True})

    @app.post("/api/file/<int:file_id>/note")
    def note(file_id: int):
        case = C()
        data = request.get_json(force=True)
        case.db.update_file(file_id, notes=data.get("notes", ""))
        case.db.commit()
        return jsonify({"ok": True})

    # ---- meta / stats / report ----------------------------
    @app.get("/api/context")
    def context():
        case = state["case"]
        if case is None:
            from .. import hashstore
            try:
                hstore = hashstore.summary()
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                # never let a bad store break the launcher
                hstore = {"sets": [], "entries": 0}
            return jsonify({"needs_case": True,
                            # capped at what push_recent ever stores (12); the
                            # launcher itself only shows 3 until "Show more"
                            "recent": appconfig.recent_cases(limit=12),
                            # the launcher says what an acquisition can be read
                            # as, from the list the walk itself is built on
                            "walked_filesystems": list(archive.WALKED_FILESYSTEMS),
                            "native": state["native"],
                            # the global reference store (NSRL etc.) isn't tied
                            # to a case, so it's reachable before opening one too
                            "known_hash": {"global_sets": hstore["sets"],
                                           "global_entries": hstore["entries"]},
                            # the app-wide default report-header logo - see
                            # /api/settings {agency_logo} and /api/report/prefs
                            "agency_logo": appconfig.get_agency_logo()})
        try:
            return jsonify(_context_payload(case))
        except Exception:  # noqa: BLE001 - a broken stat query must not blank the UI
            app.logger.exception("context payload failed")
            return jsonify({
                "needs_case": False, "native": state["native"],
                "case": case.db.get_meta("case_name"), "case_dir": str(case.root),
                "examiner": case.examiner, "sources": [], "archive_sources": [],
                "clusters": [],
                "categories": list(categories.catmap(case.db).values()),
                "flags": list(flags.flagmap(case.db).values()),
                "stats": {}, "vic": None, "errors": 0,
                "known_hash": {}, "screening": {},
                "timezone": appconfig.get_timezone(), "timezone_options": [],
            })

    def _context_payload(case) -> dict:
        srcs = [r["source"] for r in case.db.conn.execute(
            "SELECT DISTINCT source FROM files WHERE source IS NOT NULL ORDER BY source")]
        clusters = [
            {"id": r["cluster_id"], "n": r["n"]}
            for r in case.db.conn.execute(
                "SELECT cluster_id, COUNT(*) n FROM files WHERE cluster_id IS NOT NULL "
                "GROUP BY cluster_id ORDER BY n DESC LIMIT 200")
        ]
        vic = None
        if case.db.get_meta("vic_source_json"):
            vic = {k: case.db.get_meta("vic_" + k) for k in
                   ("source_json", "files_dir", "case_id", "case_number",
                    "source_app", "source_app_version")}
        from .. import detect
        scr = case.db.conn.execute(
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN faces > 0 THEN 1 ELSE 0 END) wf, "
            "SUM(CASE WHEN skin_ratio IS NOT NULL AND skin_ratio > 0 THEN 1 ELSE 0 END) ws "
            "FROM files WHERE kind IN ('image','video') AND thumb IS NOT NULL"
        ).fetchone()
        n_err = case.db.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE error IS NOT NULL").fetchone()["n"]
        arch = case.db.conn.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN id IN (SELECT container_id FROM files "
            "WHERE container_id IS NOT NULL) THEN 1 ELSE 0 END) expanded "
            "FROM files WHERE kind = 'archive' OR (kind = 'other' AND lower(ext) IN "
            "('.zip','.tar','.gz','.tgz','.bz2','.tbz2','.xz','.txz','.7z','.rar'))"
        ).fetchone()
        from .. import hashstore, stash
        cst = case.db.stats()
        try:
            hstore = hashstore.summary()
        except Exception:  # noqa: BLE001 - never let a bad store break the app
            hstore = {"sets": [], "entries": 0}
        try:
            stash_sum = stash.summary()
        except Exception:  # noqa: BLE001
            stash_sum = {"total": 0, "by_category": {}, "updated": None,
                         "path": "", "shared": False}
        from .. import timeutil
        return {
            "needs_case": False,
            "native": state["native"],
            "job": dict(state["job"]),
            "timezone": case.db.get_meta("display_tz") or appconfig.get_timezone(),
            "timezone_options": [{"value": v, "label": lbl}
                                 for v, lbl in timeutil.COMMON_ZONES],
            "case": case.db.get_meta("case_name"),
            "case_dir": str(case.root),
            "examiner": case.examiner,
            "sources": srcs,
            "archive_sources": archive.source_status(case),
            "basemap": basemaps.get_active(),
            "basemaps": len(basemaps.list_basemaps()),
            "clusters": clusters,
            "categories": list(categories.catmap(case.db).values()),
            "flags": list(flags.flagmap(case.db).values()),
            "stats": cst,
            "vic": vic,
            "errors": n_err,
            "archives": {"total": arch["total"] or 0,
                         "expanded": arch["expanded"] or 0},
            "known_hash": {
                "hits": cst.get("hashset_hits", 0),
                "known_good": cst.get("known_good", 0),
                "global_sets": hstore["sets"],
                "global_entries": hstore["entries"],
                "case_sets": [dict(r) for r in case.db.list_hashsets()],
                "stash": stash_sum,
                "use_stash": case.db.get_meta("use_stash") != "0",
                "use_vic": case.db.get_meta("use_vic") != "0",
            },
            "screening": {
                # screened_at is set by a screening pass or by an ingest with
                # screening on; fall back to "any file carries a skin_ratio",
                # which self-heals cases processed before that meta was written.
                "done": (case.db.get_meta("screened_at") is not None
                         or bool(case.db.conn.execute(
                             "SELECT 1 FROM files WHERE skin_ratio IS NOT NULL LIMIT 1"
                         ).fetchone())),
                "backend": detect.face_backend(),
                "with_faces": scr["wf"] or 0,
                "with_skin": scr["ws"] or 0,
                "screenable": scr["n"] or 0,
            },
        }

    # ---- offline basemaps -------------------------------------------
    @app.get("/api/basemaps")
    def basemaps_list():
        return jsonify({"active": basemaps.get_active(), "basemaps": basemaps.list_basemaps()})

    @app.post("/api/basemaps/import")
    def basemaps_import():
        """Copy a .pmtiles or raster .mbtiles under the app's data folder, hashing it on
        the way; runs as a job because the file can be gigabytes."""
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        body = request.get_json(force=True) or {}
        raw = str(body.get("path", "")).strip().strip('"')
        if not raw or not Path(raw).is_file():
            abort(400, description=f"file not found: {raw or '(none)'}")
        try:
            basemaps.inspect(raw)                    # refuse before any copying starts
        except (OSError, ValueError, sqlite3.Error) as exc:
            abort(400, description=str(exc))
        name = str(body.get("name", "")).strip() or None
        state["job"] = {"running": True, "stage": "process", "done": 0, "total": 0,
                        "message": f"Importing basemap {Path(raw).name}…", "stats": None,
                        "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                rec = basemaps.import_basemap(
                    raw, name=name, progress=lambda d, t: j.update(done=d, total=t))
                j.update(running=False, stage="done",
                         message=f"Basemap {rec['name']} imported",
                         stats={"basemap": rec["name"]})
            except (OSError, ValueError, sqlite3.Error) as exc:
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/basemaps/remove")
    def basemaps_remove():
        name = str((request.get_json(force=True) or {}).get("name", ""))
        if not basemaps.remove_basemap(name):
            abort(404, description=f"no basemap named {name!r}")
        return jsonify({"ok": True, "active": basemaps.get_active()})

    @app.post("/api/basemaps/active")
    def basemaps_active():
        name = (request.get_json(force=True) or {}).get("name") or None
        try:
            basemaps.set_active(name)
        except ValueError as exc:
            abort(404, description=str(exc))
        return jsonify({"ok": True, "active": basemaps.get_active()})

    @app.get("/api/basemaps/style")
    def basemaps_style():
        """A MapLibre style for the active (or named) basemap; every URL in it is local."""
        name = request.args.get("name") or basemaps.get_active()
        if not name:
            abort(404, description="no basemap imported")
        try:
            return jsonify(basemaps.style(name, flavor=request.args.get("flavor", "dark")))
        except (ValueError, OSError) as exc:
            abort(404, description=str(exc))

    @app.post("/api/basemaps/used")
    def basemaps_used():
        """The case records which basemap its map was drawn on, so the report can say."""
        case = C()
        name = str((request.get_json(force=True) or {}).get("name", "")) or basemaps.get_active()
        rec = basemaps.record_use(case, name) if name else None
        return jsonify({"ok": rec is not None, "name": rec["name"] if rec else None,
                        "sha256": rec["sha256"] if rec else None})

    @app.get("/basemap/<name>/<int:z>/<int:x>/<int:y>")
    def basemap_tile(name: str, z: int, x: int, y: int):
        """One raster tile from an MBTiles basemap, addressed the XYZ way."""
        rec = basemaps.get(name)
        if not rec or rec["format"] != basemaps.FORMAT_MBTILES:
            abort(404)
        got = basemaps.mbtiles_tile(rec["path"], z, x, y)
        if got is None:
            abort(404)
        data, mime = got
        resp = app.response_class(data, mimetype=mime)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.get("/basemap/<path:filename>")
    def basemap_file(filename: str):
        """The PMTiles file itself, with byte ranges, which is all pmtiles.js needs."""
        p = basemaps.basemap_dir() / filename
        rec = basemaps.get(Path(filename).stem)
        if (not rec or rec["format"] != basemaps.FORMAT_PMTILES
                or Path(rec["path"]) != p or not p.is_file()):
            abort(404)                  # an MBTiles is served tile by tile, never whole
        return send_file(p, mimetype="application/octet-stream", conditional=True)

    # ---- archive sources (extraction zips) -------------------------
    @app.get("/api/sources")
    def sources_status():
        return jsonify(archive.source_status(C()))

    @app.post("/api/source/relink")
    def source_relink():
        """Point an archive source at the zip's new location. Accepted only when
        the file holds every registered member with the same size and CRC."""
        case = C()
        body = request.get_json(force=True) or {}
        name = str(body.get("name", ""))
        raw = str(body.get("path", "")).strip().strip('"')
        if not name or not raw:
            abort(400, description="name and path are required")
        try:
            status = archive.relink_source(case, name, raw)
        except ValueError as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, **status})

    @app.post("/api/source/stage")
    def source_stage():
        """Copy a reference-mode source's files under the case (self-contained)."""
        case = C()
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        name = str((request.get_json(force=True) or {}).get("name", ""))
        if archive.source_record(case, name) is None:
            abort(404, description=f"{name!r} is not an archive source of this case")
        state["job"] = {"running": True, "stage": "process", "done": 0, "total": 0,
                        "message": f"Copying {name} into the case…", "stats": None,
                        "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                n = archive.stage_source(
                    case, name, progress=lambda d, t: j.update(done=d, total=t))
                j.update(running=False, stage="done",
                         message=f"{n:,} files copied into the case",
                         stats={"staged": n})
            except (archive.ArchiveUnavailable, ValueError, OSError,
                    sqlite3.Error) as exc:
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/source/unstage")
    def source_unstage():
        """Drop a staged source's copies and read from the zip on demand again."""
        case = C()
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        name = str((request.get_json(force=True) or {}).get("name", ""))
        try:
            n = archive.unstage_source(case, name)
        except (archive.ArchiveUnavailable, ValueError) as exc:
            abort(400, description=str(exc))
        return jsonify({"ok": True, "removed": n})

    @app.post("/api/source/carve")
    def source_carve():
        """Carve an already-walked disk image (E01 or raw) for deleted media, then
        process the new rows. Runs as a background job; the bottom bar follows it."""
        case = C()
        if state["job"]["running"]:
            abort(409, description="a job is already running")
        name = str((request.get_json(force=True) or {}).get("name", ""))
        rec = archive.source_record(case, name)
        if rec is None:
            abort(404, description=f"{name!r} is not an archive source of this case")
        if rec["format"] not in archive.IMAGE_FORMATS:
            abort(400, description="only a disk image (E01 or raw) can be carved")
        state["job"] = {"running": True, "stage": "carve", "done": 0, "total": 0,
                        "message": f"Recovering deleted media from {name}…",
                        "stats": None, "error": None}

        def _job() -> None:
            j = state["job"]
            try:
                # First recover deleted files from deleted records (NTFS MFT,
                # FAT32 and exFAT directory entries), by name and reaching
                # resident NTFS files a carve cannot, then carve the free space
                # by signature for whatever no surviving record names, skipping
                # the offsets already recovered here.
                recovered, offsets = archive.recover_deleted(
                    case, name,
                    progress=lambda k: j.update(done=k, message=f"Recovering deleted files… {k:,}"))
                added = archive.carve_source(
                    case, name, unallocated_only=True, extra_skip=offsets,
                    progress=lambda k: j.update(done=k, message=f"Carving… {k:,} found"))
                total_new = recovered + added
                if total_new:
                    j.update(stage="process", done=0, total=total_new,
                             message=f"Processing {total_new:,} recovered file(s)…")
                    process(case, where="md5 IS NULL", reason="carve-source",
                            progress=lambda d, t: j.update(done=d, total=t),
                            stage_cb=lambda m: j.update(message=m))
                parts = []
                if recovered:
                    parts.append(f"{recovered:,} recovered from deleted records")
                if added:
                    parts.append(f"{added:,} carved")
                j.update(running=False, stage="done",
                         message=("; ".join(parts) if parts else "Nothing new was recovered"),
                         stats={"recovered": recovered, "carved": added})
            except (ValueError, archive.ArchiveUnavailable, OSError,
                    sqlite3.Error) as exc:
                j.update(running=False, stage="error",
                         error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True})

    @app.get("/api/stats")
    def stats():
        return jsonify(C().db.stats())

    @app.get("/api/audit")
    def audit_list():
        """The case processing history / audit log, newest first."""
        try:
            limit = min(int(request.args.get("limit", 500)), 5000)
        except (TypeError, ValueError):
            limit = 500
        return jsonify(C().db.iter_audit(limit=limit))

    @app.post("/api/settings")
    def settings_update():
        """Currently just the display timezone (applied to shown epoch times,
        never to EXIF/Captured). Persisted per case and as the app-wide default."""
        from .. import timeutil
        body = request.get_json(silent=True) or {}
        if "timezone" in body:
            tz = str(body.get("timezone") or "UTC").strip() or "UTC"
            if not timeutil.is_known(tz):
                abort(400, description=f"unknown timezone: {tz}")
            case = state["case"]
            if case is not None:
                case.db.set_meta("display_tz", "" if tz == "UTC" else tz)
                case.db.audit_log(case.examiner, "set_timezone", tz)
            appconfig.set_timezone(tz)
            return jsonify({"ok": True, "timezone": tz,
                            "label": timeutil.label(tz)})
        if "use_stash" in body:
            # per-case: skip the examiner's hash stash for a case that isn't
            # CSAM/Project VIC related, where a stashed hit would be noise
            case = state["case"]
            if case is None:
                abort(409, description="no case open")
            on = bool(body.get("use_stash"))
            case.db.set_meta("use_stash", "1" if on else "0")
            cleared = 0
            if not on:
                # turning it off should not leave stale stash badges/flags
                # behind - clear them immediately rather than waiting on a
                # manual Re-check. The category it may have set is left
                # alone, same as any other hash-set hit that stops matching.
                # A file the stash flagged along with another source keeps
                # that source.
                from .. import stash
                cleared = case.db.drop_sources(name=stash.STASH_NAME)
            case.db.audit_log(case.examiner, "set_use_stash",
                              "on" if on else f"off, {cleared} stash hit(s) cleared")
            return jsonify({"ok": True, "use_stash": on, "cleared": cleared})
        if "use_vic" in body:
            # per-case: skip every Project VIC hash set, for a case that has
            # nothing to do with them
            case = state["case"]
            if case is None:
                abort(409, description="no case open")
            on = bool(body.get("use_vic"))
            case.db.set_meta("use_vic", "1" if on else "0")
            cleared = 0
            if not on:
                cleared = case.db.drop_sources(src="vic")
            case.db.audit_log(case.examiner, "set_use_vic",
                              "on" if on else f"off, {cleared} Project VIC hit(s) cleared")
            return jsonify({"ok": True, "use_vic": on, "cleared": cleared})
        if "agency_logo" in body:
            # the app-wide default report-header logo (reachable pre-case, from
            # the launcher's Settings menu) - every case's report uses it
            # unless that case sets its own, from its own Export dialog
            # (see _REPORT_HEADER_KEYS / /api/report/prefs)
            logo = body.get("agency_logo")
            if logo and not (isinstance(logo, str) and logo.startswith("data:image/")):
                abort(400, description="agency_logo must be a data:image/... URI")
            if isinstance(logo, str) and len(logo) > 4_000_000:
                abort(400, description="logo too large - pick a smaller image")
            appconfig.set_agency_logo(logo or None)
            return jsonify({"ok": True, "agency_logo": appconfig.get_agency_logo()})
        return jsonify({"ok": True})

    def _scope_where(body: dict) -> tuple[str, str]:
        """(where_sql, human_label) for a report/export scope selection, narrowed
        by ``vic_match``: "only" keeps the files that matched a Project VIC
        hash-set record, "exclude" leaves them out (to keep an HTML report small,
        say). A match is a file whose matched entry came from a Project VIC
        record, so a plain hash list's match is not one."""
        where, label = _base_scope_where(body)
        vic = {"only": ("hashset_vic IS NOT NULL", "Project VIC matches only"),
               "exclude": ("hashset_vic IS NULL", "Project VIC matches left out"),
               }.get(body.get("vic_match"))
        if not vic:
            return where, label
        clause = f"({where}) AND {vic[0]}" if where else vic[0]
        return clause, f"{label}, {vic[1]}"

    def _base_scope_where(body: dict) -> tuple[str, str]:
        scope = body.get("scope", "all")
        if scope == "selected":
            ids = [int(x) for x in (body.get("ids") or [])]
            if not ids:
                abort(400, description="no files selected")
            return f"id IN ({','.join(map(str, ids))})", f"{len(ids)} selected"
        if scope == "categorized":
            return "category != 0", "categorized only"
        if scope == "uncategorized":
            return "category = 0", "uncategorized only"
        if scope == "flags":
            return "id IN (SELECT file_id FROM file_flags)", "flagged only"
        if scope == "specificflags":
            codes = sorted({int(x) for x in (body.get("flags") or [])})
            if not codes:
                abort(400, description="no flags selected")
            fmap = flags.flagmap(C().db)
            names = ", ".join(fmap.get(c, {}).get("name", f"Flag {c}") for c in codes)
            return (f"id IN (SELECT file_id FROM file_flags WHERE flag_code IN "
                    f"({','.join(map(str, codes))}))", names)
        if scope == "categories":
            codes = sorted({int(x) for x in (body.get("categories") or [])})
            if not codes:
                abort(400, description="no categories selected")
            names = ", ".join(categories.label(C().db, c) for c in codes)
            return f"category IN ({','.join(map(str, codes))})", names
        if scope == "where" and body.get("where"):
            return str(body["where"]), "custom filter"
        return "", "all files"

    _REPORT_HEADER_KEYS = ("agency", "logo", "case_number", "item_number",
                           "examiner", "notes")

    @app.get("/api/report/prefs")
    def report_prefs():
        """Saved report header + per-image field selection for this case."""
        case = C()
        raw = case.db.get_meta("report_header")
        header = {}
        if raw:
            try:
                header = json.loads(raw)
            except ValueError:
                header = {}
        header.setdefault("examiner", case.examiner)
        if not header.get("logo"):
            # this case has never set its own - fall back to the app-wide
            # default (☰ Settings on the launcher); exporting as-is adopts it
            # as this case's own from then on, same as any other edit here
            header["logo"] = appconfig.get_agency_logo() or ""
        raw_f = case.db.get_meta("report_fields")
        try:
            fields = json.loads(raw_f) if raw_f else None
        except ValueError:
            fields = None
        return jsonify({
            "header": header,
            "fields": fields or report.DEFAULT_REPORT_FIELDS,
            "field_options": [{"key": k, "label": v[0]}
                              for k, v in report._FIELD_DEFS.items()],
            "full_images": case.db.get_meta("report_full_images") != "0",
            "full_videos": case.db.get_meta("report_full_videos") != "0",
            "maps": case.db.get_meta("report_maps") != "0",
            "blur": case.db.get_meta("report_blur") != "0",
        })

    @app.post("/api/report")
    def make_report():
        case = C()
        body = request.get_json(silent=True) or {}
        fmts = body.get("format", ["html", "csv", "json"])
        where, label = _scope_where(body)
        by_flag = body.get("scope") in ("flags", "specificflags")
        only_flags = ([int(x) for x in body.get("flags") or []]
                     if body.get("scope") == "specificflags" else None)
        out = case.report_dir

        rh = body.get("report_header")
        header = None
        if isinstance(rh, dict):
            header = {k: rh[k] for k in _REPORT_HEADER_KEYS if rh.get(k)} or None
        fields = body.get("fields") or None
        full_images = body.get("full_images", True)
        full_videos = body.get("full_videos", True)
        want_maps = body.get("maps", True)
        blur = body.get("blur", True)
        if header is not None:  # remember for next time (logo can be large - cap it)
            store = dict(header)
            if isinstance(store.get("logo"), str) and len(store["logo"]) > 4_000_000:
                store.pop("logo", None)
            case.db.set_meta("report_header", json.dumps(store))
        if fields:
            case.db.set_meta("report_fields",
                             json.dumps([f for f in fields
                                         if f in report._FIELD_DEFS]))
        case.db.set_meta("report_full_images", "1" if full_images else "0")
        case.db.set_meta("report_full_videos", "1" if full_videos else "0")
        case.db.set_meta("report_maps", "1" if want_maps else "0")
        case.db.set_meta("report_blur", "1" if blur else "0")

        tz = case.db.get_meta("display_tz") or appconfig.get_timezone()

        tag = "" if not where else "_" + {
            "categorized only": "categorized",
            "uncategorized only": "uncategorized",
            "flagged only": "flags",
            "all files, Project VIC matches only": "vicmatches",
            "all files, Project VIC matches left out": "novicmatches",
        }.get(label, "selection")

        # A LAVA project stages every file's media, draws a locator map for each
        # geolocated one and copies the frames out of every video, so it is
        # minutes of work on a real case rather than the seconds the other
        # formats take. It runs as a job with the bottom bar following it, and
        # the formats picked alongside it run in the same job.
        if "lava" in fmts:
            if state["job"]["running"]:
                abort(409, description="a job is already running")
            state["job"] = {"running": True, "stage": "export", "done": 0,
                            "total": 0, "message": "Building the LAVA report…",
                            "stats": None, "error": None}

            def _job() -> None:
                j = state["job"]
                try:
                    written = _write_reports(
                        case, fmts, out, tag, where, label, header=header,
                        fields=fields, full_images=full_images,
                        full_videos=full_videos, maps=want_maps, tz=tz, blur=blur,
                        by_flag=by_flag, only_flags=only_flags,
                        only_categorized=body.get("scope") == "categorized"
                        or bool(body.get("only_categorized")),
                        progress=lambda d, t: j.update(done=d, total=t),
                        stage_cb=lambda m: j.update(message=m))
                    j.update(running=False, stage="done",
                             message="Export complete",
                             stats={"written": written, "report_dir": str(out)})
                # any writer failing has to reach the bar rather than the log
                except Exception as exc:  # pylint: disable=broad-except
                    j.update(running=False, stage="error",
                             error=f"{type(exc).__name__}: {exc}")

            threading.Thread(target=_job, daemon=True).start()
            return jsonify({"ok": True, "job": True, "scope": label,
                            "dir": str(out)})

        try:
            made = _write_reports(
                case, fmts, out, tag, where, label, header=header, fields=fields,
                full_images=full_images, full_videos=full_videos,
                maps=want_maps, tz=tz, blur=blur, by_flag=by_flag, only_flags=only_flags,
                only_categorized=body.get("scope") == "categorized"
                or bool(body.get("only_categorized")))
        except FileNotFoundError as exc:
            return jsonify({"error": "no_vic", "message": str(exc)}), 400
        return jsonify({"ok": True, "written": made, "dir": str(out), "scope": label})

    @app.post("/api/export/md5")
    def export_md5_only():
        case = C()
        body = request.get_json(silent=True) or {}
        where, label = _scope_where(body)
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = case.report_dir / f"md5_{ts}.csv"
        report.export_md5(case, dest, where)
        n = sum(1 for _ in open(dest, encoding="utf-8")) - 1
        return jsonify({"ok": True, "path": str(dest), "count": n, "scope": label})

    # ---- snapshots / backups --------------------------------------
    @app.get("/api/save-state")
    def save_state():
        case = state["case"]
        return jsonify({
            "dirty": bool(getattr(case.db, "dirty", False)) if case else False,
            "last_write": case.db.last_write if case else 0,
            "last_backup": state["last_backup"],
            "auto_interval_min": backup.AUTO_INTERVAL_MIN,
        })

    @app.get("/api/snapshots")
    def snapshots_list():
        return jsonify([s.__dict__ for s in backup.list_snapshots(C())])

    @app.post("/api/snapshot")
    def snapshot_now():
        case = C()
        label = (request.get_json(silent=True) or {}).get("label") or None
        snap = backup.snapshot(case, label)
        state["last_backup"] = time.time()
        case.db.audit_log(case.examiner, "snapshot", json.dumps(
            {"name": snap.name, "size": snap.size, "reason": "manual", "label": label}))
        return jsonify({"ok": True, **snap.__dict__})

    @app.post("/api/snapshot/restore")
    def snapshot_restore():
        import shutil
        case = C()
        if state["job"]["running"]:
            abort(409, description="finish the running job first")
        name = str((request.get_json(force=True) or {}).get("name", ""))
        try:
            src = backup.snapshot_path(case, name)
        except ValueError as exc:
            abort(400, description=str(exc))
        root = case.root
        # 1. safety net - the current state becomes a snapshot, so this is undoable
        try:
            pre = backup.snapshot(case, "pre-restore")
            case.db.audit_log(TOOL_ACTOR, "snapshot", json.dumps(
                {"name": pre.name, "size": pre.size, "reason": "pre-restore"}))
        except Exception:  # noqa: BLE001
            pass
        # 2. close the live DB, swap the file in, reopen
        case.close()
        state["case"] = None
        try:
            for ext in ("-wal", "-shm"):
                (root / f"case.gleapp{ext}").unlink(missing_ok=True)
            shutil.copy2(src, root / "case.gleapp")
        except OSError as exc:
            state["case"] = open_case(root)      # put the case back as it was
            abort(500, description=f"could not replace the case file: {exc}")
        state["case"] = open_case(root)
        state["last_backup"] = 0.0
        state["case"].db.audit_log(state["case"].examiner,
                                   "restore_snapshot", json.dumps({"name": name}))
        return jsonify({"ok": True, "restored": name})

    return app


# Every format the export route can write. The gallery's export dialog offers
# one checkbox per entry, and a test holds the two together: the LAVA report was
# written by the route months before the dialog offered it, so the one output
# that carries the media and the location maps into LAVA could only be made from
# the command line.
REPORT_FORMATS = ("html", "csv", "json", "kml", "md5", "vic", "lava")


def _write_reports(case, fmts, out, tag, where, label, *, header, fields,
                   full_images, full_videos, maps, tz, only_categorized, blur=True,
                   by_flag=False, only_flags=None,
                   progress=None, stage_cb=None) -> list[str]:
    """Write every picked format into ``out`` and return the paths written.

    Shared by the inline route and the job the LAVA format runs in, so the two
    cannot drift into writing different things for the same request.
    """
    made: list[str] = []
    if "csv" in fmts:
        made.append(str(report.export_csv(case, out / f"report{tag}.csv", where, tz=tz)))
    if "json" in fmts:
        made.append(str(report.export_json(case, out / f"report{tag}.json", where,
                                           header=header)))
    if "html" in fmts:
        made.append(str(report.export_html(
            case, out / f"report{tag}.html", where,
            header=header, fields=fields, scope_label=label,
            full_images=full_images, full_videos=full_videos, tz=tz, maps=maps,
            blur=blur, by_flag=by_flag, only_flags=only_flags)))
    if "kml" in fmts:
        made.append(str(report.export_kml(case, out / f"geolocation{tag}.kmz", where)))
    if "md5" in fmts:
        made.append(str(report.export_md5(case, out / f"md5{tag}.csv", where)))
    if "vic" in fmts:
        made.append(str(report.export_projectvic(
            case, out / "projectvic_export.json",
            only_categorized=only_categorized)))
    if "lava" in fmts:
        if stage_cb:
            stage_cb("Building the LAVA report…")
        made.append(str(lava.export_lava(case, out / f"lava{tag}", where,
                                         maps=maps, tz_name=tz, progress=progress)))
    return made


def _run_job(state: dict, sources, opts: dict) -> None:
    job = state["job"]
    case = state["case"]
    job.update(running=True, stage="ingest", done=0, total=0,
               message="Scanning sources…", stats=None, error=None)
    try:
        n = ingest_sources(
            case, sources,
            progress=lambda k: job.update(done=k, message=f"Registering files… {k:,}"),
            expand_archives=bool(opts.get("expand_archives", True)),
        )
        if n:
            # now that the case has content, it's worth remembering
            appconfig.push_recent(str(case.root),
                                  case.db.get_meta("case_name") or case.root.name)

        # A disk image was just walked file by file. If carving was asked for, also
        # scan the space no volume claims for deleted media, before processing so
        # the carved rows are hashed and thumbnailed in the same pass.
        if opts.get("carve"):
            for src in sources:
                if src.kind != "archive":
                    continue
                rec = archive.source_record(case, src.name)
                if not rec or rec["format"] not in archive.IMAGE_FORMATS:
                    continue
                job.update(stage="carve", done=0, total=0,
                           message=f"Recovering deleted media from {src.name}…")
                recovered, offsets = archive.recover_deleted(
                    case, src.name,
                    progress=lambda k, _s=src.name: job.update(
                        done=k, message=f"Recovering deleted files from {_s}… {k:,}"))
                n += recovered + archive.carve_source(
                    case, src.name, unallocated_only=True, extra_skip=offsets,
                    progress=lambda k, _s=src.name: job.update(
                        done=k, message=f"Carving {_s}… {k:,} found"))

        job.update(stage="process", total=n, message=f"Processing {n} files…")

        def progress(done: int, total: int) -> None:
            job.update(done=done, total=total)

        stats = process(
            case,
            force=opts.get("force", False),
            workers=int(opts.get("workers", 4)),
            keyframes=int(opts.get("keyframes", 6)),
            screen=opts.get("screen", True),
            phash_cluster_threshold=int(opts.get("cluster_threshold", 8)),
            progress=progress,
            stage_cb=lambda msg: job.update(message=msg),
        )
        job.update(running=False, stage="done", message="Done",
                   stats=stats.as_dict())
    except Exception as exc:  # noqa: BLE001
        job.update(running=False, stage="error",
                   error=f"{type(exc).__name__}: {exc}")
