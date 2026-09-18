"""GLEAPP command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, hashdb, lava, report
from .case import open_case, parse_source_spec
from .pipeline import ingest_sources, process
from .similar import find_similar


def _p(msg: str) -> None:
    print(msg, flush=True)


def cmd_init(args: argparse.Namespace) -> int:
    case = open_case(args.case, create=True, examiner=args.examiner)
    if args.name:
        case.db.set_meta("case_name", args.name)
    _p(f"Initialised case '{case.db.get_meta('case_name')}' at {case.root}")
    case.close()
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    case = open_case(args.case, create=True, examiner=args.examiner)
    sources, meta = parse_source_spec(args.source)
    if meta.get("case"):
        case.db.set_meta("case_name", str(meta["case"]))
    if meta.get("examiner"):
        case.examiner = str(meta["examiner"])
    if args.stage:
        for s in sources:
            if s.kind == "archive":
                s.stage = True
    _p(f"Sources ({len(sources)}):")
    for s in sources:
        how = ""
        if s.kind == "archive":
            how = "  [copied into the case]" if s.stage else "  [read from the archive on demand]"
        _p(f"  - {s.name}: {s.path}{how}")
    n = ingest_sources(case, sources)
    _p(f"Registered {n} files.")
    if not args.no_process:
        _run_process(case, args)
    case.close()
    return 0


def _run_process(case, args) -> None:
    def progress(done: int, total: int) -> None:
        pct = 100 * done // max(total, 1)
        print(f"\r  processing {done}/{total} ({pct}%)", end="", flush=True)

    _p("Processing...")
    stats = process(
        case,
        force=args.force,
        workers=args.workers,
        keyframes=args.keyframes,
        screen=not args.no_screen,
        phash_cluster_threshold=args.cluster_threshold,
        progress=progress,
    )
    print()
    _p(json.dumps(stats.as_dict(), indent=2))


def cmd_process(args: argparse.Namespace) -> int:
    case = open_case(args.case, examiner=args.examiner)
    _run_process(case, args)
    case.close()
    return 0


def cmd_hashset(args: argparse.Namespace) -> int:
    from . import hashstore

    if args.rm is not None:
        hashstore.delete_set(args.rm)
        _p(f"Removed global hash set {args.rm}.")
        return 0

    if args.list:
        s = hashstore.summary()
        _p(f"Global hash store: {s['entries']:,} entries across {len(s['sets'])} set(s)")
        for hs in s["sets"]:
            pdna = int(hs.get("photodna") or 0)
            _p(f"  [{hs['id']}] {hs['name']}  {hs['count']:,}  ({hs['kind']})"
               + (f"  [{pdna:,} PhotoDNA, not matched]" if pdna else ""))
        return 0

    # NSRL RDSv3 helpers: build a .db from a full .sql dump, and/or apply a delta
    src = args.file
    if args.schema and args.full:
        out = Path(args.full).with_suffix(".db")
        _p(f"Building {out.name} from {Path(args.schema).name} + {Path(args.full).name} …")
        src = str(hashstore.build_db(args.schema, args.full, out_db=out))
    if args.delta:
        base = src or args.base
        if not base:
            _p("error: --delta needs a base full .db (positional arg or --base)")
            return 2
        _p(f"Applying {Path(args.delta).name} onto a copy of {Path(base).name} …")
        src = str(hashstore.apply_delta(base, args.delta))
        _p(f"  updated database: {src}")

    if not src:
        _p("error: give a hash list (.db / VIC json / CSV / text), or --list, "
           "or --schema/--full/--delta to build one")
        return 2

    algos = tuple(a.strip() for a in args.algos.split(",")) if args.algos else None
    name = args.name or Path(src).stem
    last = [0.0]
    import time as _time

    def prog(seen: int, added: int) -> None:
        now = _time.monotonic()
        if now - last[0] >= 5:
            last[0] = now
            _p(f"  … {seen:,} rows scanned, {added:,} unique hashes stored")

    if args.to_global:
        hs_id, added = hashstore.import_path(
            src, name=name, kind=args.kind, table=args.table,
            algos=algos, progress=prog)
        _p(f"Imported '{name}' into the global store: {added:,} hashes.")
        note = hashdb.photodna_note(hashstore.algo_counts(hs_id))
    else:
        case = open_case(args.case, create=True, examiner=args.examiner)
        hs_id, added = hashdb.import_hashset(
            case.db, src, name=name, kind=args.kind, actor=case.examiner)
        _p(f"Imported hash set '{name}' into the case: {added:,} entries.")
        note = hashdb.photodna_note(hashdb.algo_counts(case.db.conn, hs_id))
        case.close()
    if note:
        _p(f"  {note}")
    return 0


def cmd_stash(args: argparse.Namespace) -> int:
    """Manage the local hash stash (examiner's own category 1-3 MD5s).

    Its own portable file, separate from the global/NSRL store - share it by
    ``--export`` + a colleague's ``--merge``, or point a team at one file with
    ``--set-path`` (or $GLEAPP_STASH_PATH).
    """
    from . import stash

    if args.set_path is not None:
        p = stash.set_path(args.set_path or None)
        _p(f"Stash file is now: {p}")
        return 0

    if args.clear:
        n = stash.clear()
        _p(f"Local hash stash cleared — {n:,} entries removed.")
        return 0

    if args.export:
        dest = stash.export(Path(args.export))
        _p(f"Wrote the stash to {dest}")
        return 0

    if args.merge:
        res = stash.merge(Path(args.merge))
        _p(f"Merged {Path(args.merge).name}: +{res['added']:,} new, "
           f"{res['total']:,} total.")
        return 0

    if args.add:
        case = open_case(args.case, examiner=args.examiner)
        label = case.db.get_meta("case_name") or Path(args.case).name
        ph = ",".join("?" * len(stash.STASH_CATEGORIES))
        rows = case.db.conn.execute(
            f"SELECT md5, category FROM files WHERE category IN ({ph}) "
            "AND md5 IS NOT NULL AND md5 != ''",
            tuple(stash.STASH_CATEGORIES)).fetchall()
        res = stash.add((r["md5"], r["category"], label) for r in rows)
        case.db.audit_log(args.examiner, "stash_add",
                          f"{res['submitted']} md5s, {res['added']} new")
        case.close()
        _p(f"Stash: +{res['added']:,} new ({res['submitted']:,} submitted), "
           f"{res['total']:,} total.")
        return 0

    s = stash.summary()
    _p(f"Local hash stash: {s['total']:,} MD5(s)"
       + (f", updated {s['updated']:.0f}" if s["updated"] else " (empty)"))
    _p(f"  file: {s['path']}" + ("  (shared)" if s["shared"] else ""))
    for code, n in sorted(s["by_category"].items()):
        _p(f"  category {code}: {n:,}")
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    """Archive sources: list them, relink one whose zip moved, copy one into the
    case, or drop the copies again."""
    from . import archive

    if args.action != "list" and not args.name:
        raise ValueError(f"'source {args.action}' needs a source name (see 'source list')")
    if args.action == "relink" and not args.path:
        raise ValueError("'source relink' needs the zip's new path")
    case = open_case(args.case, examiner=args.examiner)
    try:
        if args.action == "list":
            rows = archive.source_status(case)
            if not rows:
                _p("No archive sources in this case.")
            for s in rows:
                _p(f"  {s['name']}: {s['files']:,} files, {s['format']}, {s['mode']}, "
                   f"{s['status']}  {s['path']}")
            return 0
        if args.action == "relink":
            s = archive.relink_source(case, args.name, args.path)
            _p(f"{s['name']} now reads from {s['path']} ({s['status']}).")
            return 0
        if args.action == "stage":
            def progress(done: int, total: int) -> None:
                print(f"\r  copying {done}/{total}", end="", flush=True)

            n = archive.stage_source(case, args.name, progress=progress)
            print()
            _p(f"Copied {n:,} files into the case; {args.name} is self-contained now.")
            return 0
        if args.action == "carve":
            def carve_progress(done: int) -> None:
                print(f"\r  {done:,} files", end="", flush=True)

            recovered, offsets = archive.recover_deleted(
                case, args.name, progress=carve_progress)
            print()
            n = archive.carve_source(case, args.name,
                                     unallocated_only=args.unallocated_only,
                                     extra_skip=offsets, progress=carve_progress)
            print()
            _p(f"Recovered {recovered:,} deleted file(s) from deleted records (with their "
               f"name, NTFS/FAT32/exFAT, resident NTFS files included) and {n:,} by signature "
               f"carving (no name or date) from {args.name}; run 'process' to hash and "
               f"thumbnail them.")
            return 0
        n = archive.unstage_source(case, args.name)
        _p(f"Removed {n:,} copies; {args.name} is read from the archive on demand again.")
        return 0
    except archive.ArchiveUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        case.close()


def cmd_maps(args: argparse.Namespace) -> int:
    """Offline basemaps: list, import, remove, choose the active one, or cut a region."""
    from . import basemaps

    if args.action == "list":
        rows = basemaps.list_basemaps()
        if not rows:
            _p("No basemaps imported. 'gleapp maps import FILE' takes a .pmtiles or a "
               "raster .mbtiles; 'gleapp maps extract' cuts a region of the Protomaps build.")
        for b in rows:
            mark = "*" if b["active"] else " "
            gone = "" if b["present"] else "  (file missing)"
            _p(f"{mark} {b['name']}: {b['format']}, {b['size']:,} bytes, zoom "
               f"{b['min_zoom']}-{b['max_zoom']}, sha256 {b['sha256']}{gone}")
        return 0
    if args.action == "import":
        if not args.path:
            raise ValueError("'maps import' needs the basemap file to import")
        last = [0.0]

        def progress(done: int, total: int) -> None:
            import time as _time
            if _time.monotonic() - last[0] >= 1 or done == total:
                last[0] = _time.monotonic()
                print(f"\r  copying {done / 1e6:,.0f} / {total / 1e6:,.0f} MB", end="", flush=True)

        rec = basemaps.import_basemap(args.path, name=args.name, progress=progress)
        print()
        _p(f"Imported {rec['name']} ({rec['format']}, zoom {rec['min_zoom']}-{rec['max_zoom']}), "
           f"sha256 {rec['sha256']}")
        _p(f"  active basemap: {basemaps.get_active()}")
        return 0
    if args.action in ("remove", "use"):
        target = args.path or args.name
        if not target:
            raise ValueError(f"'maps {args.action}' needs the basemap's name (see 'maps list')")
        if args.action == "remove":
            if not basemaps.remove_basemap(target):
                raise ValueError(f"no basemap named {target!r}")
            _p(f"Removed {target}.")
        else:
            basemaps.set_active(target)
            _p(f"Active basemap: {target}")
        return 0
    # extract: a region of the Protomaps planet build, through the pmtiles tool
    import shutil
    import subprocess
    if not args.bbox or not args.out:
        raise ValueError("'maps extract' needs --bbox=W,S,E,N and --out FILE.pmtiles")
    build = args.build or "https://build.protomaps.com/YYYYMMDD.pmtiles"
    cmd = ["pmtiles", "extract", build, args.out, f"--bbox={args.bbox}"]
    if args.maxzoom is not None:
        cmd.append(f"--maxzoom={args.maxzoom}")
    tool = shutil.which("pmtiles")
    if tool is None or not args.build:
        _p("Run this with the pmtiles tool (https://github.com/protomaps/go-pmtiles/releases),"
           " replacing YYYYMMDD with a recent build date:" if not args.build
           else "The pmtiles tool is not on PATH; download it from "
                "https://github.com/protomaps/go-pmtiles/releases and run:")
        _p("  " + " ".join(cmd))
        _p("Then: gleapp maps import " + args.out)
        return 0 if tool or not args.build else 2
    _p("  " + " ".join(cmd))
    rc = subprocess.run(cmd, check=False).returncode
    if rc == 0:
        _p(f"Wrote {args.out}. Import it with: gleapp maps import {args.out}")
    return rc


def cmd_stats(args: argparse.Namespace) -> int:
    case = open_case(args.case)
    _p(json.dumps(case.db.stats(), indent=2))
    case.close()
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    from .detect import face_backend
    from .pipeline import screen_pass

    case = open_case(args.case, examiner=args.examiner)
    _p(f"Face/skin screening (backend: {face_backend()})…")

    def progress(done: int, total: int) -> None:
        print(f"\r  {done}/{total}", end="", flush=True)

    n = screen_pass(case, workers=args.workers, progress=progress)
    print()
    _p(f"Screened {n} files.")
    case.close()
    return 0


def cmd_similar(args: argparse.Namespace) -> int:
    case = open_case(args.case)
    hits = find_similar(case, args.file_id, threshold=args.threshold, limit=args.limit)
    for h in hits:
        _p(f"{h['distance']:>3}  {h['similarity']:>5}%  [{h['id']}] {h['rel_path']}")
    _p(f"{len(hits)} similar file(s).")
    case.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    case = open_case(args.case)
    where = args.where or {
        "categorized": "category != 0",
        "uncategorized": "category = 0",
        "flags": "id IN (SELECT file_id FROM file_flags)",
    }.get(args.scope, "")
    tag = f"_{args.scope}" if args.scope != "all" and not args.where else ""
    # a file whose match came from a Project VIC hash-set record
    vic = {"only": "hashset_vic IS NOT NULL", "exclude": "hashset_vic IS NULL"}.get(
        args.vic_matches)
    if vic:
        where = f"({where}) AND {vic}" if where else vic
        tag += "_vicmatches" if args.vic_matches == "only" else "_novicmatches"
    fields = None
    if args.no_vic_details:
        fields = [k for k in report.DEFAULT_REPORT_FIELDS if not k.startswith("vic_")]
    out_dir = case.report_dir
    made = []
    fmts = args.format or ["html", "csv", "json"]
    if "csv" in fmts:
        made.append(report.export_csv(case, out_dir / f"report{tag}.csv", where))
    if "json" in fmts:
        made.append(report.export_json(case, out_dir / f"report{tag}.json", where))
    if "html" in fmts:
        made.append(report.export_html(case, out_dir / f"report{tag}.html", where,
                                       full_images=not args.thumbs_only,
                                       full_videos=not args.thumbs_only,
                                       maps=not args.no_maps, fields=fields,
                                       by_flag=args.scope == "flags"))
    if "kml" in fmts:
        made.append(report.export_kml(case, out_dir / f"geolocation{tag}.kmz", where))
    if "md5" in fmts:
        made.append(report.export_md5(case, out_dir / f"md5{tag}.csv", where))
    if "vic" in fmts:
        made.append(report.export_projectvic(
            case, out_dir / "projectvic_export.json",
            only_categorized=args.scope == "categorized"))
    if "lava" in fmts:
        # A folder rather than a file: LAVA opens a project, and the media it shows
        # lives beside the manifest.
        def prog(done: int, total: int) -> None:
            if total and (done == total or done % 250 == 0):
                _p(f"  media {done}/{total}")
        made.append(lava.export_lava(case, out_dir / f"lava{tag}", where,
                                     thumbs=args.lava_thumbs, link=args.link,
                                     maps=not args.no_maps,
                                     keyframes=not args.no_keyframes, progress=prog))
    for m in made:
        _p(f"  wrote {m}")
    case.close()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from .web.app import create_app

    has_case = (Path(args.case) / "case.gleapp").exists()
    app = create_app(args.case if has_case else None)
    url = f"http://{args.host}:{args.port}"
    _p(f"GLEAPP review UI -> {url}  (Ctrl-C to stop)")
    if not has_case:
        _p("No case at that path yet - the launcher will open in the browser.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    from .desktop import main as desktop_main

    has_case = (Path(args.case) / "case.gleapp").exists()
    return desktop_main([args.case] if has_case else [])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gleapp", description="GLEAPP media forensics toolkit")
    ap.add_argument("--version", action="version", version=f"GLEAPP {__version__}")
    ap.add_argument("-c", "--case", default="case", help="case directory (default: ./case)")
    ap.add_argument("--examiner", default=None,
                    help="examiner name for the audit log (default: keep the case's "
                         "stored name, or 'examiner' for a brand-new case)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create an empty case")
    s.add_argument("--name")
    s.set_defaults(func=cmd_init)

    def add_proc_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--force", action="store_true", help="reprocess already-done files")
        sp.add_argument("--workers", type=int, default=4)
        sp.add_argument("--keyframes", type=int, default=6, help="frames per video")
        sp.add_argument("--no-screen", action="store_true", help="skip face/skin screening")
        sp.add_argument("--cluster-threshold", type=int, default=8)

    s = sub.add_parser("ingest", help="ingest a folder, a full-file-system extraction archive "
                                      "(zip or tar), an E01 acquisition, or a JSON spec, "
                                      "then process")
    s.add_argument("source", help="folder path, extraction .zip or .tar "
                                 "(plain, .gz, .bz2 or .xz), .E01 "
                                  "acquisition, OR .json spec file")
    s.add_argument("--no-process", action="store_true", help="register files only")
    s.add_argument("--stage", action="store_true",
                   help="copy the media out of an extraction archive into the case, so the "
                        "case is self-contained (default: read it from the archive on demand; "
                        "a compressed tar is always copied out)")
    add_proc_opts(s)
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("source",
                       help="extraction archives: list them, relink a moved one, copy one "
                            "into the case (stage) and back (unstage), or carve an "
                            "acquisition for what a walk of it could not reach")
    s.add_argument("action",
                   choices=["list", "relink", "stage", "unstage", "carve"])
    s.add_argument("name", nargs="?", help="source name, as shown by 'source list'")
    s.add_argument("path", nargs="?", help="relink: where the archive is now")
    s.add_argument("--unallocated-only", action="store_true",
                   help="carve: scan only the space no volume claims, which is the "
                        "only part a walk cannot reach. Falls back to the whole "
                        "image when a volume cannot report its free space, rather "
                        "than leaving part of the disk unscanned")
    s.set_defaults(func=cmd_source)

    s = sub.add_parser("process", help="(re)run processing on the current case")
    add_proc_opts(s)
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("hashset",
                       help="import a known-hash list (SQLite / NSRL RDSv3 / VIC / CSV)")
    s.add_argument("file", nargs="?", help="hash list (.db/.json/.csv/.txt); "
                                           "omit with --list or --schema/--full")
    s.add_argument("--name")
    s.add_argument("--kind", choices=["known", "known-good", "other"], default="known")
    s.add_argument("--global", dest="to_global", action="store_true",
                   help="import into the shared global store (all cases use it)")
    s.add_argument("--table", help="SQLite: table/view with the hash columns "
                                   "(default: auto - METADATA / FILE / DISTINCT_HASH)")
    s.add_argument("--algos", help="comma list to import, e.g. sha256,sha1 "
                                   "(default: all of md5,sha1,sha256 present)")
    s.add_argument("--schema", help="NSRL: <set>.schema.sql (with --full)")
    s.add_argument("--full", help="NSRL: full <set>.sql data dump (built into a .db)")
    s.add_argument("--base", help="NSRL: previous full <set>.db to apply --delta onto")
    s.add_argument("--delta", help="NSRL: <set>_delta.sql to merge onto the base .db")
    s.add_argument("--list", action="store_true", help="list the global store's sets")
    s.add_argument("--rm", type=int, metavar="ID", help="remove a global set by id")
    s.set_defaults(func=cmd_hashset)

    s = sub.add_parser("stash",
                       help="local hash stash: your category 1-3 MD5s, re-used across cases")
    s.add_argument("--add", action="store_true",
                   help="add the current case's category 1-3 MD5s to the stash")
    s.add_argument("--clear", action="store_true", help="erase the entire stash")
    s.add_argument("--export", metavar="FILE",
                   help="write the stash to FILE (.csv = hash list, else a portable .hstash copy)")
    s.add_argument("--merge", metavar="FILE",
                   help="fold another examiner's stash (.csv, .hstash, or .gleapp) into yours")
    s.add_argument("--set-path", metavar="PATH", default=None,
                   help="use a different stash file (e.g. on a shared drive); "
                        "pass '' to reset to the per-user default")
    s.set_defaults(func=cmd_stash)

    s = sub.add_parser("maps",
                       help="offline basemaps for the gallery map: list, import a .pmtiles or "
                            "raster .mbtiles, remove, choose the active one, or cut a region "
                            "of the Protomaps build")
    s.add_argument("action", choices=["list", "import", "remove", "use", "extract"])
    s.add_argument("path", nargs="?", help="import: the basemap file; remove/use: its name")
    s.add_argument("--name", help="import: the name to file it under (default: the file name)")
    s.add_argument("--bbox", help="extract: W,S,E,N in decimal degrees, written --bbox=W,S,E,N "
                                  "(a western longitude starts with a minus sign)")
    s.add_argument("--out", help="extract: the .pmtiles file to write")
    s.add_argument("--maxzoom", type=int, help="extract: highest zoom to keep (default: all, 15)")
    s.add_argument("--build", help="extract: the planet build URL, e.g. "
                                   "https://build.protomaps.com/YYYYMMDD.pmtiles")
    s.set_defaults(func=cmd_maps)

    s = sub.add_parser("stats", help="print case statistics")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("screen", help="run face/skin screening on an existing case")
    s.add_argument("--workers", type=int, default=4)
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("similar", help="list files perceptually similar to FILE_ID")
    s.add_argument("file_id", type=int)
    s.add_argument("--threshold", type=int, default=12)
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_similar)

    s = sub.add_parser("report", help="export CSV/JSON/HTML/KML/MD5-list/VIC")
    s.add_argument("--format", nargs="+",
                   choices=["csv", "json", "html", "kml", "md5", "vic", "lava"])
    s.add_argument("--scope", default="all",
                   choices=["all", "categorized", "uncategorized", "flags"],
                   help="which files to include; 'flags' also groups the HTML "
                        "report by flag instead of category (default: all)")
    s.add_argument("--where", help="raw SQL filter on the files table (overrides --scope)")
    s.add_argument("--vic-matches", default="include",
                   choices=["include", "only", "exclude"],
                   help="files whose hash matched a Project VIC hash-set record: "
                        "keep them (default), report only them, or leave them out, "
                        "for example to keep an HTML report small")
    s.add_argument("--no-vic-details", action="store_true",
                   help="HTML report: leave the Project VIC record MediaID, series, "
                        "flags, tags and Exif out of the fields under each image")
    s.add_argument("--thumbs-only", action="store_true",
                   help="HTML report: thumbnails only - no full-size images or videos")
    s.add_argument("--no-maps", action="store_true",
                   help="HTML and LAVA reports: skip the location maps (drawn from "
                        "the active basemap)")
    s.add_argument("--lava-thumbs", action="store_true",
                   help="LAVA report: put GLEAPP's thumbnail in the Media column "
                        "rather than the file, for a report meant to travel")
    s.add_argument("--no-keyframes", action="store_true",
                   help="LAVA report: skip the frames extracted from each video")
    s.add_argument("--link", action="store_true",
                   help="LAVA report: hardlink the media instead of copying it. Only "
                        "for a report staying on the volume it was built on: the "
                        "report then shares an inode with the evidence")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("web", help="launch the review gallery in a browser")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8749)
    s.add_argument("--debug", action="store_true")
    s.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("desktop", help="launch the review gallery in a native window")
    s.set_defaults(func=cmd_desktop)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
