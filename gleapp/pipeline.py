"""End-to-end processing pipeline.

    ingest -> hash -> metadata -> thumbnails/keyframes -> visual screen
    -> hash-set match -> stack -> cluster

Every stage writes straight to the case DB and is safe to re-run: files already
fully processed are skipped unless ``force=True``.
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import dedupe, detect, hashdb, imaging, lzc  # noqa: F401  (imaging: decoder setup)
from .case import Case, Source
from .hashing import crypto_hashes, perceptual_hashes
from .ingest import scan
from .media import extract_video_isolated, make_image_thumb
from .metadata import best_created_dt, extract_image


@dataclass
class RunStats:
    discovered: int = 0
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    redundant_duplicates: int = 0
    visual_stacks: int = 0
    clusters: int = 0
    hashset_hits: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def ingest_sources(case: Case, sources: list[Source]) -> int:
    """Discover files from each source and register them in the DB."""
    from . import projectvic

    n = 0
    for src in sources:
        if src.kind == "projectvic":
            reg, missing = projectvic.import_vic(
                case, src.path, files_dir=src.files_dir
            )
            n += reg + missing
            continue
        for d in scan(
            src.path,
            include_other=src.include_other,
            follow_symlinks=src.follow_symlinks,
            max_bytes=src.max_bytes,
        ):
            case.db.upsert_file(
                d.path,
                rel_path=d.rel_path,
                source=src.name,
                kind=d.kind,
                ext=d.ext,
                size=d.size,
                mtime=d.mtime,
                ctime=d.ctime,
            )
            n += 1
    case.db.commit()
    case.db.audit_log(case.examiner, "ingest",
                      f"{n} files from {len(sources)} source(s)")
    return n


def _process_one(thumb_dir, row, *, force: bool, keyframes: int, screen: bool) -> dict:
    """Pure worker: no DB access. Returns a payload for the writer thread.

    payload = {"id", "status": ok|skip|error, "fields": {...}, "keyframes": [...]}
    """
    fid = row["id"]
    path = row["path"]
    keys = row.keys()
    if row["md5"] and row["thumb"] and not force:
        return {"id": fid, "status": "skip", "fields": {}, "keyframes": []}
    if row["error"] == "file not found on disk" and not Path(path).exists():
        return {"id": fid, "status": "skip", "fields": {}, "keyframes": []}

    mtime = row["mtime"] if "mtime" in keys else None
    ctime = row["ctime"] if "ctime" in keys else None
    existing_dt = row["created_dt"] if "created_dt" in keys else None

    def keep_dt(new):  # don't clobber a good imported date with nothing
        return new or existing_dt

    upd: dict = {}
    kfs: list[tuple[float, str, str | None]] = []
    try:
        # trust the hash from a Project VIC import; only hash if we don't have one
        if not row["md5"] or force:
            upd.update(crypto_hashes(path))

        # A Snapchat "LZC" bundle isn't itself an image/video - pull the best
        # embedded media out to a sidecar file and process that instead.
        kind, decode_path = row["kind"], path
        if kind == "archive" or lzc.is_lzc(path):
            got = lzc.extract_best(path)
            if got is None:
                return {"id": fid, "status": "error", "keyframes": [], "fields": {
                    "error": "Snapchat LZC bundle - no displayable media inside",
                    "kind": "other"}}
            ekind, eext, edata = got
            ex_dir = Path(thumb_dir).parent / "extracted"
            ex_dir.mkdir(parents=True, exist_ok=True)
            decode_path = str(ex_dir / f"{fid}.{eext}")
            Path(decode_path).write_bytes(edata)
            kind = ekind
            upd["kind"] = ekind

        if kind == "image":
            im = imaging.load_for_processing(decode_path)   # Pillow/HEIC/KTX
            try:
                upd.update(perceptual_hashes(im))
                meta = extract_image(decode_path, im)
                upd.update({k: v for k, v in meta.items() if v is not None})
                upd["created_dt"] = keep_dt(best_created_dt(
                    meta.get("created_dt"), mtime, ctime))
                thumb = make_image_thumb(path, thumb_dir, im)
                if thumb:
                    upd["thumb"] = thumb
                if screen:
                    upd.update(detect.screen(im))
            finally:
                im.close()

        elif kind == "video":
            upd["created_dt"] = keep_dt(best_created_dt(None, mtime, ctime))
            # isolated: a corrupt/partial clip can hard-crash the decoder
            res = extract_video_isolated(decode_path, thumb_dir, count=keyframes,
                                         screen=screen)
            if res is None:
                upd["error"] = "video could not be decoded (corrupt or unsupported)"
                return {"id": fid, "status": "error", "fields": upd, "keyframes": []}
            for k, v in res.get("info", {}).items():
                if v is not None:
                    upd[k] = v
            frames = res.get("frames", [])
            if frames:
                mid = len(frames) // 2
                upd["thumb"] = frames[mid]["name"]
                upd["phash"] = frames[mid]["phash"]
                for fr in frames:
                    kfs.append((fr["ts"], fr["name"], fr["phash"]))
                if screen:
                    upd["faces"] = res.get("faces", 0)
                    upd["skin_ratio"] = res.get("skin_ratio", 0.0)
        else:
            upd["created_dt"] = keep_dt(best_created_dt(None, mtime, ctime))

        upd["error"] = None
        return {"id": fid, "status": "ok", "fields": upd, "keyframes": kfs}
    except Exception as exc:  # noqa: BLE001 - record and continue
        return {
            "id": fid, "status": "error",
            "fields": {"error": imaging.describe_failure(path, exc)},
            "keyframes": [],
        }


def _screen_one(thumb_dir: Path, row) -> dict:
    """Face + skin screening from the (already generated) thumbnail - fast."""
    from PIL import Image as _Img

    src = thumb_dir / row["thumb"] if row["thumb"] else Path(row["path"])
    try:
        with _Img.open(src) as im:
            im.load()
            s = detect.screen(im)
        return {"id": row["id"], "faces": s["faces"],
                "skin_ratio": s["skin_ratio"]}
    except Exception:  # noqa: BLE001
        return {"id": row["id"], "faces": 0, "skin_ratio": 0.0}


def screen_pass(case: Case, *, workers: int = 4, progress=None) -> int:
    """Run face/skin screening over every thumbnailed image & video, in place.

    Cheap enough to run on its own after a fast (screening-off) processing run.
    Returns the number of files updated.
    """
    rows = [r for r in case.db.iter_files(
        "kind IN ('image','video') AND thumb IS NOT NULL")]
    total = len(rows)
    n = [0]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(lambda r: _screen_one(case.thumb_dir, r), rows):
            case.db.update_file(res["id"], faces=res["faces"],
                                skin_ratio=res["skin_ratio"])
            n[0] += 1
            if n[0] % 50 == 0 or n[0] == total:
                case.db.commit()
                if progress:
                    progress(n[0], total)
    case.db.commit()
    case.db.set_meta("screened_at", str(time.time()))
    case.db.audit_log(case.examiner, "screen_pass",
                      f"{n[0]} files, backend={detect.face_backend()}")
    return n[0]


def _video_result_to_payload(row, res: dict | None, *, screen: bool) -> dict:
    fid = row["id"]
    keys = row.keys()
    mtime = row["mtime"] if "mtime" in keys else None
    ctime = row["ctime"] if "ctime" in keys else None
    existing_dt = row["created_dt"] if "created_dt" in keys else None
    upd: dict = {"created_dt": (best_created_dt(None, mtime, ctime) or existing_dt)}
    if not row["md5"]:
        try:
            upd.update(crypto_hashes(row["path"]))
        except OSError as exc:
            return {"id": fid, "status": "error",
                    "fields": {"error": f"{exc}"[:300]}, "keyframes": []}
    if res is None:
        upd["error"] = "video could not be decoded (corrupt or unsupported)"
        return {"id": fid, "status": "error", "fields": upd, "keyframes": []}
    for k, v in (res.get("info") or {}).items():
        if v is not None:
            upd[k] = v
    frames = res.get("frames") or []
    if not frames:
        upd["error"] = "no video frames could be read (truncated or unsupported)"
        return {"id": fid, "status": "error", "fields": upd, "keyframes": []}
    mid = len(frames) // 2
    upd["thumb"] = frames[mid]["name"]
    upd["phash"] = frames[mid]["phash"]
    kfs = [(f["ts"], f["name"], f["phash"]) for f in frames]
    if screen:
        upd["faces"] = res.get("faces", 0)
        upd["skin_ratio"] = res.get("skin_ratio", 0.0)
    upd["error"] = None
    return {"id": fid, "status": "ok", "fields": upd, "keyframes": kfs}


def _process_videos(case: Case, vids, *, force, keyframes, screen, workers, write):
    from .media import extract_videos_batch

    todo = []
    for r in vids:
        if r["md5"] and r["thumb"] and not force:
            write({"id": r["id"], "status": "skip", "fields": {}, "keyframes": []})
        elif r["error"] == "file not found on disk" and not Path(r["path"]).exists():
            write({"id": r["id"], "status": "skip", "fields": {}, "keyframes": []})
        else:
            todo.append(r)
    if not todo:
        return

    by_path = {r["path"]: r for r in todo}
    paths = list(by_path)
    batch = 20
    chunks = [paths[i:i + batch] for i in range(0, len(paths), batch)]
    lock = threading.Lock()

    def do_chunk(chunk):
        results = extract_videos_batch(chunk, case.thumb_dir, count=keyframes,
                                       screen=screen)
        with lock:
            for p in chunk:
                write(_video_result_to_payload(by_path[p], results.get(p),
                                               screen=screen))

    with ThreadPoolExecutor(max_workers=min(max(2, workers), 4)) as ex:
        list(ex.map(do_chunk, chunks))


def process(
    case: Case,
    *,
    force: bool = False,
    workers: int = 4,
    keyframes: int = 6,
    screen: bool = True,
    phash_cluster_threshold: int = 12,
    where: str = "",
    progress=None,
    stage_cb=None,
) -> RunStats:
    def stage(msg: str) -> None:
        if stage_cb:
            stage_cb(msg)

    stats = RunStats()
    rows = case.db.iter_files(where)
    stats.discovered = len(rows)
    total = len(rows)
    done = [0]

    def _write(res: dict) -> None:
        if res["status"] == "ok":
            stats.processed += 1
        elif res["status"] == "skip":
            stats.skipped += 1
        else:
            stats.errors += 1
        if res["fields"]:
            case.db.update_file(res["id"], **res["fields"])
        if res["keyframes"]:
            case.db.conn.execute(
                "DELETE FROM keyframes WHERE file_id=?", (res["id"],))
            for ts, name, ph in res["keyframes"]:
                case.db.add_keyframe(res["id"], ts, name, ph)
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == total:
            case.db.commit()
            if progress:
                progress(done[0], total)

    # LZC bundles are unpacked in-process by _process_one even when they hold a
    # video, so keep them out of the batched video path.
    vids = [r for r in rows if r["kind"] == "video" and not lzc.is_lzc(r["path"])]
    _vid_ids = {r["id"] for r in vids}
    imgs = [r for r in rows if r["id"] not in _vid_ids]

    # images/other: threads (fast, in-process)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_process_one, case.thumb_dir, r, force=force,
                          keyframes=keyframes, screen=screen) for r in imgs]
        for fut in as_completed(futs):
            _write(fut.result())
    case.db.commit()

    # videos: batched child processes (each decode is isolated from the run)
    if vids:
        _process_videos(case, vids, force=force, keyframes=keyframes,
                        screen=screen, workers=workers, write=_write)
    case.db.commit()

    # Known-hash matching (needs all hashes present).
    stage("Matching known-hash lists…")
    for r in case.db.iter_files("md5 IS NOT NULL"):
        hit = hashdb.match_file(case.db, r)
        if hit:
            case.db.update_file(
                r["id"],
                hashset_hit=hit["name"],
                hashset_cat=hit["category"],
            )
            # adopt the asserted category if the file is still uncategorized
            if hit["category"] and (r["category"] or 0) == 0 and hit["kind"] == "known":
                case.db.update_file(r["id"], category=hit["category"])
            stats.hashset_hits += 1
    case.db.commit()

    stage("Stacking exact duplicates…")
    stats.redundant_duplicates = dedupe.stack_exact(case.db)
    stage("Stacking visual matches…")
    stats.visual_stacks = dedupe.stack_visual(case.db)
    stage("Clustering near-duplicates…")
    stats.clusters = dedupe.cluster_near(case.db, threshold=phash_cluster_threshold)

    case.db.set_meta("last_run", str(time.time()))
    case.db.audit_log(case.examiner, "process", str(stats.as_dict()))
    return stats
