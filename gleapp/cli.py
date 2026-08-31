"""GLEAPP command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, hashdb, report
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
    _p(f"Sources ({len(sources)}):")
    for s in sources:
        _p(f"  - {s.name}: {s.path}")
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
    case = open_case(args.case, create=True, examiner=args.examiner)
    hs_id, added = hashdb.import_hashset(
        case.db, args.file, name=args.name, kind=args.kind
    )
    _p(f"Imported hash set '{args.name or Path(args.file).stem}': {added} entries.")
    case.close()
    return 0


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
        "reviewed": "reviewed = 1",
    }.get(args.scope, "")
    tag = f"_{args.scope}" if args.scope != "all" and not args.where else ""
    out_dir = case.report_dir
    made = []
    fmts = args.format or ["html", "csv", "json"]
    if "csv" in fmts:
        made.append(report.export_csv(case, out_dir / f"report{tag}.csv", where))
    if "json" in fmts:
        made.append(report.export_json(case, out_dir / f"report{tag}.json", where))
    if "html" in fmts:
        made.append(report.export_html(case, out_dir / f"report{tag}.html", where))
    if "kml" in fmts:
        made.append(report.export_kml(case, out_dir / f"geolocation{tag}.kml", where))
    if "md5" in fmts:
        made.append(report.export_md5(case, out_dir / f"md5{tag}.csv", where))
    if "vic" in fmts:
        made.append(report.export_projectvic(
            case, out_dir / "projectvic_export.json",
            only_categorized=args.scope == "categorized"))
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
    ap.add_argument("--examiner", default="examiner", help="examiner name for the audit log")
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

    s = sub.add_parser("ingest", help="ingest a folder or JSON spec, then process")
    s.add_argument("source", help="folder path OR .json spec file")
    s.add_argument("--no-process", action="store_true", help="register files only")
    add_proc_opts(s)
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("process", help="(re)run processing on the current case")
    add_proc_opts(s)
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("hashset", help="import a known-hash list (Project VIC / CAID / CSV)")
    s.add_argument("file")
    s.add_argument("--name")
    s.add_argument("--kind", choices=["known", "known-good", "other"], default="known")
    s.set_defaults(func=cmd_hashset)

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
                   choices=["csv", "json", "html", "kml", "md5", "vic"])
    s.add_argument("--scope", default="all",
                   choices=["all", "categorized", "uncategorized", "reviewed"],
                   help="which files to include (default: all)")
    s.add_argument("--where", help="raw SQL filter on the files table (overrides --scope)")
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
