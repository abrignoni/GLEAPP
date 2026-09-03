"""End-to-end processing pipeline.

    ingest -> hash -> metadata -> thumbnails/keyframes -> visual screen
    -> hash-set match -> stack -> cluster

Every stage writes straight to the case DB and is safe to re-run: files already
fully processed are skipped unless ``force=True``.
"""

from __future__ import annotations

import struct
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import archive, dedupe, detect, hashdb, imaging, lzc  # noqa: F401  (imaging: decoder setup)
from .case import Case, Source
from .hashing import crypto_hashes, perceptual_hashes
from .ingest import scan, sniff_kind
from .media import extract_video_isolated, make_image_thumb
from .metadata import extract_image


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


def ingest_sources(case: Case, sources: list[Source], *, progress=None) -> int:
    """Discover files from each source and register them in the DB.

    ``progress(n)`` (optional) is called every ~200 files with the running
    count, and the DB is committed at the same cadence so the web UI can show
    files as they are registered rather than only when the whole scan ends.
    """
    from . import projectvic

    n = 0
    for src in sources:
        if src.kind == "projectvic":
            reg, missing = projectvic.import_vic(
                case, src.path, files_dir=src.files_dir
            )
            n += reg + missing
            if progress:
                progress(n)
            continue
        if src.kind == "archive":
            n = archive.ingest_archive(case, src, count=n, progress=progress)
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
                atime=d.atime or None,
            )
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    case.db.commit()
    case.db.audit_log(case.examiner, "ingest",
                      f"{n} files from {len(sources)} source(s)")
    return n


def _process_one(case_root, thumb_dir, row, *, force: bool, keyframes: int, screen: bool,
                 rec: dict | None = None) -> dict:
    """Pure worker: no DB access. Returns a payload for the writer thread.

    payload = {"id", "status": ok|skip|error, "fields": {...}, "keyframes": [...]}

    ``rec`` is the archive source record for a row whose bytes live in a zip; they are
    pulled out for the duration of the work and dropped again.
    """
    fid = row["id"]
    if row["md5"] and row["thumb"] and not force:
        return {"id": fid, "status": "skip", "fields": {}, "keyframes": []}
    if row["error"] == "file not found on disk" and not Path(row["path"]).exists():
        return {"id": fid, "status": "skip", "fields": {}, "keyframes": []}
    try:
        with archive.local_copy(case_root, rec, row) as local:
            return _process_one_at(thumb_dir, row, str(local), force=force,
                                   keyframes=keyframes, screen=screen)
    except archive.ArchiveUnavailable as exc:
        return {"id": fid, "status": "error", "keyframes": [],
                "fields": {"error": f"source archive unavailable: {exc}"[:300]}}


def _process_one_at(thumb_dir, row, local: str, *, force: bool, keyframes: int,
                    screen: bool) -> dict:
    """The work of ``_process_one`` once the bytes are at ``local``. The registered
    path still names the thumbnail, so a thumbnail is the same whichever mode made it."""
    fid = row["id"]
    path = row["path"]
    keys = row.keys()

    existing_dt = row["created_dt"] if "created_dt" in keys else None

    def keep_dt(new):  # don't clobber a good imported date with nothing
        return new or existing_dt

    upd: dict = {}
    kfs: list[tuple[float, str, str | None]] = []
    try:
        # trust the hash from a Project VIC import; only hash if we don't have one
        if not row["md5"] or force:
            upd.update(crypto_hashes(local))

        # A Snapchat "LZC" bundle isn't itself an image/video - pull the best
        # embedded media out to a sidecar file and process that instead.
        kind, decode_path = row["kind"], local
        if kind == "archive" or lzc.is_lzc(local):
            got = lzc.extract_best(local)
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

        # A Project VIC import (or an app image cache) can hand us an
        # extension-less file with a vague MIME (image/unknown) as kind=other.
        # Content-sniff it by magic bytes so real images/videos still render.
        if kind == "other":
            sniffed = sniff_kind(decode_path)
            if sniffed in ("image", "video"):
                kind = sniffed
                upd["kind"] = sniffed

        if kind == "image":
            im = imaging.load_for_processing(decode_path)   # Pillow/HEIC/KTX
            try:
                upd.update(perceptual_hashes(im))
                meta = extract_image(decode_path, im)
                upd.update({k: v for k, v in meta.items() if v is not None})
                # "captured" is EXIF/embedded only - never a filesystem time
                upd["created_dt"] = keep_dt(meta.get("created_dt"))
                thumb = make_image_thumb(path, thumb_dir, im)
                if thumb:
                    upd["thumb"] = thumb
                if screen:
                    upd.update(detect.screen(im))
            finally:
                im.close()

        elif kind == "video":
            # isolated: a corrupt/partial clip can hard-crash the decoder
            res = extract_video_isolated(decode_path, thumb_dir, count=keyframes,
                                         screen=screen)
            if res is None:
                upd["error"] = _video_failure_reason(
                    decode_path, "video could not be decoded (corrupt or unsupported)")
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
        upd["error"] = None
        return {"id": fid, "status": "ok", "fields": upd, "keyframes": kfs}
    except Exception as exc:  # noqa: BLE001 - record and continue
        err = imaging.describe_failure(local, exc)
        # the source tool tells us when its own carve was incomplete
        on = (row["orig_name"] if "orig_name" in keys else "") or ""
        if "_partial" in on or "_embedded_" in on:
            err = (f"Incomplete carve by the source tool ({on}) - the embedded "
                   f"media was not fully extracted; recover it from the parent file")
        return {
            "id": fid, "status": "error",
            "fields": {"error": err},
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


def rematch_hashes(case: Case, *, progress=None) -> int:
    """Re-check every hashed file against the case + global known-hash sets.

    Standalone so it can be re-run after importing a set without a full
    reprocess.  Clears stale hits first, re-adopts an asserted category only
    when the file is still uncategorized and the set is 'known'.  Returns the
    number of current hits.
    """
    from .db import NONPERTINENT_CATEGORY
    rows = list(case.db.iter_files("md5 IS NOT NULL OR sha256 IS NOT NULL"))
    total, hits = len(rows), 0
    for i, r in enumerate(rows, 1):
        hit = hashdb.match_file(case.db, r)
        if hit:
            hits += 1
            case.db.update_file(r["id"], hashset_hit=hit["name"],
                                hashset_cat=hit["category"],
                                hashset_kind=hit["kind"])
            # auto-categorize only an as-yet-uncategorized file:
            #  - a 'known' set asserts its own category
            #  - a 'known-good' hit (NSRL etc.) -> Non-pertinent
            if (r["category"] or 0) == 0:
                if hit["kind"] == "known" and hit["category"]:
                    case.db.update_file(r["id"], category=hit["category"])
                elif hit["kind"] == "known-good":
                    case.db.update_file(r["id"], category=NONPERTINENT_CATEGORY)
        elif r["hashset_hit"] is not None:
            case.db.update_file(r["id"], hashset_hit=None, hashset_cat=None,
                                hashset_kind=None)
        if i % 500 == 0 or i == total:
            case.db.commit()
            if progress:
                progress(i, total)
    case.db.commit()
    case.db.audit_log(case.examiner, "rematch_hashes", f"{hits} hits / {total} files")
    return hits


def _mp4_box_names(raw: bytes, limit: int = 40) -> list[str]:
    i, out = 0, []
    while i + 8 <= len(raw) and len(out) < limit:
        try:
            size = struct.unpack_from(">I", raw, i)[0]
        except struct.error:
            break
        out.append(raw[i + 4:i + 8].decode("latin1", "replace"))
        if size < 8:
            break
        i += size
    return out


def _video_failure_reason(path: str, fallback: str) -> str:
    """Say *why* a video wouldn't decode - most of these are Snapchat's
    segmented streaming cache (an init segment plus byte-range fragments),
    not standalone playable files."""
    if str(path).lower().endswith(".stream_0_offset_key"):
        return "Snapchat streamed-video fragment - one chunk of a segmented download, not a whole clip"
    try:
        with open(path, "rb") as fh:
            raw = fh.read(8192)
    except OSError:
        return fallback
    if raw[:2] in (b"\xff\xf3", b"\xff\xf2", b"\xff\xfb"):
        return "Audio-frame fragment - contains no video"
    if raw[4:8] == b"ftyp":
        names = _mp4_box_names(raw)
        has_moov, has_mdat = "moov" in names, "mdat" in names
        if has_moov and not has_mdat:
            return "Fragmented-MP4 init segment - the media data lives in separate fragment files"
        if has_mdat and not has_moov:
            return "MP4 media data with no header - can't be decoded without its init segment"
        if "moof" in names:
            return "Fragmented MP4 - incomplete (missing fragments) or unsupported by the decoder"
    return fallback


def _video_result_to_payload(row, res: dict | None, *, screen: bool,
                             local: str | None = None) -> dict:
    """``local`` is where the bytes were read from, when that is not the registered
    path (a reference-mode archive row)."""
    fid = row["id"]
    keys = row.keys()
    src = local or row["path"]
    existing_dt = row["created_dt"] if "created_dt" in keys else None
    # capture time comes only from embedded metadata (res["info"]) below;
    # filesystem timestamps are not a capture time.
    upd: dict = {"created_dt": existing_dt}
    if not row["md5"]:
        try:
            upd.update(crypto_hashes(src))
        except OSError as exc:
            err = imaging.scrub_local_paths(str(exc), row["path"]) or type(exc).__name__
            return {"id": fid, "status": "error",
                    "fields": {"error": err[:300]}, "keyframes": []}
    if res is None:
        upd["error"] = _video_failure_reason(
            src, "video could not be decoded (corrupt or unsupported)")
        return {"id": fid, "status": "error", "fields": upd, "keyframes": []}
    for k, v in (res.get("info") or {}).items():
        if v is not None:
            upd[k] = v
    frames = res.get("frames") or []
    if not frames:
        upd["error"] = _video_failure_reason(
            src, "no video frames could be read (truncated or unsupported)")
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

    recs = archive.source_records(case)
    batch = 20
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    lock = threading.Lock()

    def do_chunk(rows):
        # A reference-mode archive row is pulled out of its zip for the batch and
        # dropped after; the child process reads whatever path it is handed.
        with archive.local_copies(case.root, recs, rows) as (local, failed):
            by_local = {str(local[r["id"]]): r for r in rows if r["id"] in local}
            results = extract_videos_batch(list(by_local), case.thumb_dir, count=keyframes,
                                           screen=screen) if by_local else {}
            with lock:
                for p, r in by_local.items():
                    write(_video_result_to_payload(r, results.get(p), screen=screen,
                                                   local=p))
                for r in rows:
                    if r["id"] in failed:
                        write({"id": r["id"], "status": "error", "keyframes": [],
                               "fields": {"error": f"source archive unavailable: "
                                                   f"{failed[r['id']]}"[:300]}})

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
    recs = archive.source_records(case)
    archive.clear_tmp(case.root)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_process_one, case.root, case.thumb_dir, r, force=force,
                          keyframes=keyframes, screen=screen, rec=recs.get(r["source"]))
                for r in imgs]
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
    stats.hashset_hits = rematch_hashes(case)

    stage("Stacking exact duplicates…")
    stats.redundant_duplicates = dedupe.stack_exact(case.db)
    stage("Stacking visual matches…")
    stats.visual_stacks = dedupe.stack_visual(case.db)
    stage("Clustering near-duplicates…")
    stats.clusters = dedupe.cluster_near(case.db, threshold=phash_cluster_threshold)

    # screening ran inline with processing (screen=True) - record it so the UI
    # doesn't keep offering "Run screening" for a collection that's already done
    if screen and not where:
        case.db.set_meta("screened_at", str(time.time()))

    case.db.set_meta("last_run", str(time.time()))
    case.db.audit_log(case.examiner, "process", str(stats.as_dict()))
    return stats
