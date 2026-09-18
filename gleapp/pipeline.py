"""End-to-end processing pipeline.

    ingest -> hash -> metadata -> thumbnails/keyframes -> visual screen
    -> hash-set match -> stack -> cluster

Every stage writes straight to the case DB and is safe to re-run: files already
fully processed are skipped unless ``force=True``.
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import archive, dedupe, detect, hashdb, imaging, lzc, nested, winsearch  # noqa: F401  (imaging: decoder setup)
from .case import Case, Source
from .hashing import crypto_hashes, perceptual_hashes
from .ingest import scan, sniff_kind
from .media import extract_video_isolated, make_image_thumb
from .metadata import extract_image

# process()'s "reason" values, purely to label its audit-log entry so the
# processing-history panel can tell an ingest-time run apart from a scoped
# rerun over the same case. The default "" (an empty/unscoped `where`) means
# a normal ingest-time run and is shown as "ingest".
PROCESS_REASONS = ("retry-errors", "expand-archives", "carve-source")


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
    # Per-stage outcome: stage name -> "ok" or "failed: <reason>". A stage that
    # raises is recorded here and the run continues, so one broken stage does not
    # discard the record that the earlier ones succeeded.
    stages: dict = field(default_factory=dict)

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
    case.db.audit_log(case.examiner, "ingest", json.dumps({
        "count": n,
        # the source's own declared name only - never its path or basename,
        # which is the evidence folder/file's real name on the examiner's own
        # disk (this is also written into the LAVA export; see test_export_paths)
        "sources": [{"name": s.name, "kind": s.kind} for s in sources],
    }))

    # A .zip / .tar / .gz sitting inside a source is registered as a container;
    # open it now so its media is processed in the same pass.
    added = nested.expand_containers(
        case, progress=(lambda k: progress(n + k)) if progress else None,
        include_other=any(getattr(s, "include_other", False) for s in sources))

    # A cached thumbnail extracted just above has no name of its own; if this
    # source also carried a Windows Search index, try to name it. Best-effort:
    # a source with no index, or an unreadable one, must not fail the ingest.
    try:
        winsearch.correlate_thumbnails(case)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass
    return n + added


def _process_one(case_root, thumb_dir, row, *, force: bool, keyframes: int, screen: bool,
                 rec: dict | None = None) -> dict:
    """Pure worker: no DB access. Returns a payload for the writer thread.

    payload = {"id", "status": ok|skip|error, "fields": {...}, "keyframes": [...]}

    ``rec`` is the archive source record for a row whose bytes live in a zip; they are
    pulled out for the duration of the work and dropped again.
    """
    fid = row["id"]
    if row["md5"] and not force and (row["thumb"] or row["kind"] == "archive"):
        return {"id": fid, "status": "skip", "fields": {}, "keyframes": []}
    if row["error"] == "file not found on disk" and not Path(row["path"]).exists():
        return {"id": fid, "status": "skip", "fields": {}, "keyframes": []}
    try:
        with archive.local_copy(case_root, rec, row) as local:
            return _process_one_at(thumb_dir, row, str(local), force=force,
                                   keyframes=keyframes, screen=screen)
    except archive.ArchiveUnavailable as exc:
        why = imaging.scrub_local_paths(str(exc), rec["path"] if rec else None)
        return {"id": fid, "status": "error", "keyframes": [],
                "fields": {"error": f"source archive unavailable: {why}"[:300]}}


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
    kfs: list[tuple[float, str, str | None, list[dict]]] = []
    faces_detected: list[dict] = []
    try:
        # A Project VIC import supplies the hashes the exporting tool recorded, and
        # those are trusted rather than recomputed. Which hashes arrive is up to the
        # exporter: one measured export carried MD5 and SHA1 on every entry, another
        # carried MD5 and wrote SHA1 as an empty string on all 19,209 of its entries.
        # crypto_hashes computes all three in one read, so ask for that read whenever
        # any of them is missing. Testing only MD5 left SHA1 and SHA256 empty for the
        # whole case, and hashdb matches sha256 -> sha1 -> md5, so a case imported
        # from such an export could only ever match a known-hash set on MD5.
        if force or not (row["md5"] and row["sha1"] and row["sha256"]):
            upd.update(crypto_hashes(local))

        # A Snapchat "LZC" bundle isn't itself an image/video - pull the best
        # embedded media out to a sidecar file and process that instead.
        kind, decode_path = row["kind"], local
        if lzc.is_lzc(local):
            got = lzc.extract_best(local)
            if got is None:
                return {"id": fid, "status": "error", "keyframes": [], "fields": {
                    **upd,
                    "error": "Snapchat LZC bundle - no displayable media inside",
                    "kind": "other"}}
            ekind, eext, edata = got
            ex_dir = Path(thumb_dir).parent / "extracted"
            ex_dir.mkdir(parents=True, exist_ok=True)
            decode_path = str(ex_dir / f"{fid}.{eext}")
            Path(decode_path).write_bytes(edata)
            kind = ekind
            upd["kind"] = ekind
        elif kind == "archive":
            # A real .zip / .tar / .gz container: its media members were pulled
            # out and registered separately (gleapp/nested.py). Nothing here to
            # decode - keep the hashes computed above and leave it as a container.
            # Its error column belongs to that expansion pass: a message there says
            # why the members are not in the case, and hashing the container here
            # has no bearing on that, so it stays. Only this pass's own earlier
            # message (the source archive unavailable last time) is cleared.
            if not nested.is_expansion_error(row["error"]):
                upd["error"] = None
            return {"id": fid, "status": "ok", "fields": upd, "keyframes": []}

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
                    s = detect.screen(im)
                    faces_detected = s.pop("face_records", [])
                    upd.update(s)
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
                    # a face belongs to one key frame, not the video as a whole -
                    # the embedding crossed the subprocess boundary as base64
                    # (see _vidworker.run), decode it back to real bytes here
                    kf_faces = fr.get("face_records", [])
                    for fc in kf_faces:
                        if fc.get("embedding"):
                            fc["embedding"] = base64.b64decode(fc["embedding"])
                    kfs.append((fr["ts"], fr["name"], fr["phash"], kf_faces))
                if screen:
                    upd["faces"] = res.get("faces", 0)
                    upd["skin_ratio"] = res.get("skin_ratio", 0.0)
        upd["error"] = None
        return {"id": fid, "status": "ok", "fields": upd, "keyframes": kfs,
                "faces_detected": faces_detected}
    except Exception as exc:  # noqa: BLE001 - record and continue
        err = imaging.describe_failure(local, exc)
        # the source tool tells us when its own carve was incomplete
        on = (row["orig_name"] if "orig_name" in keys else "") or ""
        if "_partial" in on or "_embedded_" in on:
            err = (f"Incomplete carve by the source tool ({on}) - the embedded "
                   f"media was not fully extracted; recover it from the parent file")
        # Keep whatever this call already derived, the hashes above especially. A
        # file GLEAPP cannot decode is exactly the one an examiner wants to look up
        # in a known-hash set, and returning only the error text left an
        # undecodable file with no MD5, SHA1 or SHA256 at all.
        return {
            "id": fid, "status": "error",
            "fields": {**upd, "error": err},
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
    case.db.audit_log(case.examiner, "screen_pass", json.dumps(
        {"count": n[0], "backend": detect.face_backend()}))
    return n[0]


def rematch_hashes(case: Case, *, progress=None) -> int:
    """Re-check every hashed file against the case + global known-hash sets.

    Standalone so it can be re-run after importing a set without a full
    reprocess.  Clears stale hits first, re-adopts an asserted category only
    when the file is still uncategorized and a notable set asserts one.  Returns
    the number of current hits.

    A file is recorded against every source that flags it - a Project VIC set
    and the examiner's hash stash, say - not just the first, so being in both is
    kept. It takes the lowest (most severe) category any notable source
    asserts, and a known-good hit (the NSRL) moves it to Non-pertinent only when
    no notable source flags it too.

    Skips the examiner's hash stash for this run when the case's own
    ``use_stash`` meta flag is turned off (a non-CSAM case, say), and every
    Project VIC set when ``use_vic`` is - see ``hashdb.match_all``.
    """
    from .db import NONPERTINENT_CATEGORY
    use_stash = case.db.get_meta("use_stash") != "0"
    use_vic = case.db.get_meta("use_vic") != "0"
    rows = list(case.db.iter_files("md5 IS NOT NULL OR sha256 IS NOT NULL"))
    total, hits = len(rows), 0
    for i, r in enumerate(rows, 1):
        found = hashdb.match_all(case.db, r, use_stash=use_stash, use_vic=use_vic)
        if found:
            hits += 1
            found = hashdb.ordered_hits(found)
            top = found[0]
            # The Project VIC record the entry came from: its MediaID, series,
            # flags, tags and Exif, as that record states them. Its Exif is
            # carried as text only and never written to this file's GPS. Taken
            # from the first source that has one, so the hash stash winning the
            # headline does not hide it.
            vic = next((v for v in (hashdb.vic_record(case.db, h) for h in found)
                        if v), None)
            asserted = hashdb.asserted_category(found)
            case.db.update_file(
                r["id"], hashset_hit=top["name"],
                hashset_cat=asserted if asserted is not None else top["category"],
                hashset_kind=top["kind"],
                hashset_vic=json.dumps(vic, ensure_ascii=False) if vic else None,
                hashset_sources=hashdb.sources_json(found),
                hashset_mask=hashdb.source_mask(found))
            # auto-categorize only an as-yet-uncategorized file:
            #  - a 'known' set asserts its own category
            #  - a 'known-good' hit (NSRL etc.) -> Non-pertinent, unless a
            #    notable source flags the file as well
            if (r["category"] or 0) == 0:
                if asserted is not None:
                    case.db.update_file(r["id"], category=asserted)
                elif (not any(h["kind"] == "known" for h in found)
                      and any(h["kind"] == "known-good" for h in found)):
                    case.db.update_file(r["id"], category=NONPERTINENT_CATEGORY)
        elif (r["hashset_hit"] is not None or r["hashset_vic"] is not None
              or r["hashset_sources"] is not None):
            case.db.update_file(r["id"], hashset_hit=None, hashset_cat=None,
                                hashset_kind=None, hashset_vic=None,
                                hashset_sources=None, hashset_mask=None)
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
    if not (row["md5"] and row["sha1"] and row["sha256"]):
        try:
            upd.update(crypto_hashes(src))
        except OSError as exc:
            err = imaging.scrub_local_paths(str(exc), row["path"]) or type(exc).__name__
            return {"id": fid, "status": "error",
                    "fields": {**upd, "error": err[:300]}, "keyframes": []}
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
    kfs = []
    for f in frames:
        # a face belongs to one key frame, not the video as a whole - the
        # embedding crossed the subprocess boundary as base64 (see
        # _vidworker.run), decode it back to real bytes here
        kf_faces = f.get("face_records", [])
        for fc in kf_faces:
            if fc.get("embedding"):
                fc["embedding"] = base64.b64decode(fc["embedding"])
        kfs.append((f["ts"], f["name"], f["phash"], kf_faces))
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
                        rec = recs.get(r["source"])
                        why = imaging.scrub_local_paths(failed[r["id"]],
                                                        rec["path"] if rec else None)
                        write({"id": r["id"], "status": "error", "keyframes": [],
                               "fields": {"error": f"source archive unavailable: {why}"
                                          [:300]}})

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
    reason: str = "",   # why this run happened, for the audit log - see PROCESS_REASONS
) -> RunStats:
    stats = RunStats()

    def stage(msg: str) -> None:
        if stage_cb:
            stage_cb(msg)

    def run_stage(name: str, msg: str, fn):
        """Run one post-processing stage, recording its outcome. A stage that
        raises is logged as failed and the run carries on to the next one."""
        stage(msg)
        try:
            result = fn()
            stats.stages[name] = "ok"
            return result
        # pylint: disable=broad-exception-caught
        except Exception as exc:  # noqa: BLE001 - any stage failure, record and continue
            reason = f"{type(exc).__name__}: {exc}".replace("'", "").replace('"', "")
            stats.stages[name] = f"failed: {reason}"[:300]
            traceback.print_exc()
            return None

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
            # ON DELETE CASCADE on faces.keyframe_id drops that key frame's
            # own faces along with it - nothing stale is left behind
            case.db.conn.execute(
                "DELETE FROM keyframes WHERE file_id=?", (res["id"],))
            for ts, name, ph, kf_faces in res["keyframes"]:
                kf_id = case.db.add_keyframe(res["id"], ts, name, ph)
                if kf_faces:
                    case.db.replace_keyframe_faces(kf_id, res["id"], kf_faces)
        # only the image path populates this (opt-in screening); everything
        # else (archives, videos, errored files) just has none to write
        if res.get("faces_detected"):
            case.db.replace_faces(res["id"], res["faces_detected"])
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

    # Media processing (hashes, metadata, thumbnails/keyframes, screening) ran
    # above; record it as one stage. A per-file failure is counted in
    # stats.errors and stored on the row, not here.
    stats.stages["media_processing"] = (
        f"ok: {stats.processed} processed, {stats.skipped} skipped, "
        f"{stats.errors} errored")

    # Known-hash matching (needs all hashes present). Each of these stages
    # reports its own done/total through the same `progress` callback the
    # per-file loop above used - the caller resets to that stage's own scale,
    # so the bar sweeps 0->100% again for each stage in turn rather than
    # sitting still (or pegged at the previous stage's 100%) while it runs.
    stats.hashset_hits = run_stage(
        "hashset_match", "Matching known-hash lists…",
        lambda: rematch_hashes(case, progress=progress)) or 0

    stats.redundant_duplicates = run_stage(
        "stack_exact", "Stacking exact duplicates…",
        lambda: dedupe.stack_exact(case.db, progress=progress)) or 0
    stats.visual_stacks = run_stage(
        "stack_visual", "Stacking visual matches…",
        lambda: dedupe.stack_visual(case.db, progress=progress)) or 0
    stats.clusters = run_stage(
        "cluster_near", "Clustering near-duplicates…",
        lambda: dedupe.cluster_near(case.db, threshold=phash_cluster_threshold,
                                    progress=progress)) or 0

    # screening ran inline with processing (screen=True) - record it so the UI
    # doesn't keep offering "Run screening" for a collection that's already done
    if screen and not where:
        case.db.set_meta("screened_at", str(time.time()))

    case.db.set_meta("last_run", str(time.time()))
    detail = stats.as_dict()
    detail["scope"] = reason or "ingest"
    detail["screened"] = screen
    case.db.audit_log(case.examiner, "process", json.dumps(detail))
    return stats
