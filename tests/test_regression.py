"""Regression suite driven by the generated media collection.

``tools/make_test_media.py`` builds a tree of deliberately-varied files plus a
``manifest.json`` of expectations.  This processes the whole thing once and
checks each file behaved as the manifest says - the scenarios here are exactly
the ones that have bitten us (native decoder crashes, featureless-image false
grouping, HEIC/KTX handling, VIC import/export, EXIF, screening).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gleapp import projectvic
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def _isolate_cfg(tmp_path_factory):
    from gleapp import hashstore
    old = os.environ.get("GLEAPP_CONFIG_DIR")
    os.environ["GLEAPP_CONFIG_DIR"] = str(tmp_path_factory.mktemp("regcfg"))
    hashstore.close()
    yield
    hashstore.close()
    if old is None:
        os.environ.pop("GLEAPP_CONFIG_DIR", None)
    else:
        os.environ["GLEAPP_CONFIG_DIR"] = old


@pytest.fixture(scope="module")
def collection(tmp_path_factory):
    d = tmp_path_factory.mktemp("regmedia")
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_media.py"), str(d)],
                   check=True, capture_output=True)
    manifest = json.loads((d / "manifest.json").read_text())
    return d, manifest


@pytest.fixture(scope="module")
def processed(collection, tmp_path_factory):
    media, manifest = collection
    case = open_case(tmp_path_factory.mktemp("case") / "c", create=True, examiner="t")
    sources, _ = parse_source_spec(media / "ingest.json")
    ingest_sources(case, sources)
    stats = process(case, workers=4, keyframes=3, screen=True)
    by_rel = {r["rel_path"].replace("\\", "/"): dict(r) for r in case.db.iter_files()}
    yield case, manifest, by_rel, stats
    case.close()


def _row(by_rel: dict, rel: str) -> dict:
    assert rel in by_rel, f"{rel} was not ingested"
    return by_rel[rel]


# --------------------------------------------------------------------------
def test_everything_ingested(processed):
    _case, manifest, by_rel, _stats = processed
    missing = set(manifest["files"]) - set(by_rel)
    assert not missing, f"not ingested: {missing}"


def test_per_file_expectations(processed):
    _case, manifest, by_rel, _stats = processed
    problems = []
    for rel, meta in manifest["files"].items():
        r = by_rel.get(rel)
        if r is None:
            problems.append(f"{rel}: not ingested")
            continue
        exp = meta.get("expect", {})

        if exp.get("kind") is None and meta.get("kind"):
            if r["kind"] != meta["kind"]:
                problems.append(f"{rel}: kind {r['kind']} != {meta['kind']}")

        if exp.get("thumb") is True and not r["thumb"]:
            problems.append(f"{rel}: expected a thumbnail, got none (err={r['error']})")
        if exp.get("phash") is True and not r["phash"]:
            problems.append(f"{rel}: expected a pHash")
        if exp.get("gps") is True and r["gps_lat"] is None:
            problems.append(f"{rel}: expected GPS coords")
        if exp.get("created_year") and not str(r["created_dt"] or "").startswith(exp["created_year"]):
            problems.append(f"{rel}: created_dt {r['created_dt']} not in {exp['created_year']}")
        if exp.get("camera") and exp["camera"] not in (r["camera"] or ""):
            problems.append(f"{rel}: camera {r['camera']!r} lacks {exp['camera']!r}")

        if exp.get("error") is True and not r["error"]:
            problems.append(f"{rel}: expected an error, got none")
        if isinstance(exp.get("error"), str) and exp["error"].lower() not in (r["error"] or "").lower():
            problems.append(f"{rel}: error {r['error']!r} lacks {exp['error']!r}")
        if exp.get("error_or_thumb") and not (r["error"] or r["thumb"]):
            problems.append(f"{rel}: expected either an error or a thumbnail")

        if exp.get("no_perceptual_group"):
            if r["vstack_id"] or r["cluster_id"]:
                problems.append(f"{rel}: featureless/distinct file was grouped "
                                f"(vstack={r['vstack_id']} cluster={r['cluster_id']})")
        if exp.get("skin_ratio_min") and (r["skin_ratio"] or 0) < exp["skin_ratio_min"]:
            problems.append(f"{rel}: skin_ratio {r['skin_ratio']} < {exp['skin_ratio_min']}")

    assert not problems, "\n".join(problems)


def test_kmz_export_embeds_thumbnails(processed, tmp_path):  # pylint: disable=redefined-outer-name
    import zipfile

    from gleapp import report

    case, _manifest, by_rel, _stats = processed
    gps = [r for r in by_rel.values()
           if r["gps_lat"] is not None and r["gps_lon"] is not None and r["thumb"]]
    assert gps, "regression collection has no geolocated file with a thumbnail"

    out = report.export_kml(case, tmp_path / "geo.kml")     # .kml -> .kmz
    assert out.suffix == ".kmz" and out.exists()

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "doc.kml" in names
        kml = z.read("doc.kml").decode("utf-8")
        assert '<img src="files/' in kml
        imgs = [n for n in names if n.startswith("files/") and n.endswith(".jpg")]
        assert imgs
        for n in imgs:
            assert z.read(n)[:3] == b"\xff\xd8\xff"          # JPEG SOI


def test_exact_and_visual_grouping(processed):
    _case, _manifest, by_rel, _stats = processed
    a, b = _row(by_rel, "duplicates/orig.jpg"), _row(by_rel, "duplicates/exact_copy.jpg")
    assert a["stack_id"] and a["stack_id"] == b["stack_id"]

    v1 = _row(by_rel, "duplicates/vis_orig.jpg")
    v2 = _row(by_rel, "duplicates/vis_recompressed.jpg")
    assert v1["phash"] != v2["phash"], "visual pair should have different pHash"
    assert v1["vstack_id"] and v1["vstack_id"] == v2["vstack_id"]

    n1 = _row(by_rel, "duplicates/near_orig.jpg")
    n2 = _row(by_rel, "duplicates/near_edit.jpg")
    assert n1["cluster_id"] and n1["cluster_id"] == n2["cluster_id"]
    assert not n1["vstack_id"], "near-dup should cluster, not visual-stack"


def test_has_duplicates_filter(processed):
    """The sidebar 'Duplicates' dropdown -> /api/files?hasdup=..."""
    from gleapp.web.app import create_app
    case, _manifest, _by_rel, _stats = processed
    app = create_app(None)
    app.config["STATE"]["case"] = case          # reuse the open connection
    cl = app.test_client()

    def rels(kind: str) -> set:
        j = cl.get(f"/api/files?hasdup={kind}&limit=5000").get_json()
        return {f["rel_path"].replace("\\", "/") for f in j["files"]}

    exact = rels("exact")
    assert {"duplicates/orig.jpg", "duplicates/exact_copy.jpg"} <= exact
    assert "duplicates/near_orig.jpg" not in exact
    assert "duplicates/vis_orig.jpg" not in exact

    visual = rels("visual")
    assert {"duplicates/vis_orig.jpg", "duplicates/vis_recompressed.jpg"} <= visual

    cluster = rels("cluster")
    assert {"duplicates/near_orig.jpg", "duplicates/near_edit.jpg"} <= cluster

    anyd = rels("any")
    assert exact <= anyd and visual <= anyd and cluster <= anyd
    assert "featureless/solid_red.png" not in anyd   # distinct file, no dupes


def test_featureless_images_do_not_group(processed):
    _case, _manifest, by_rel, _stats = processed
    for rel in ("featureless/gradient_red.jpg", "featureless/gradient_blue.jpg",
                "featureless/solid_red.png", "featureless/solid_blue.png"):
        r = _row(by_rel, rel)
        assert r["vstack_id"] is None and r["cluster_id"] is None, \
            f"{rel} grouped (pHash false positive)"


def test_corrupt_media_errs_without_crashing(processed):
    _case, _manifest, by_rel, stats = processed
    # the run finished (no native crash) and the poison-pill files are flagged
    assert stats.processed > 15
    assert _row(by_rel, "corrupt/truncated.mp4")["error"]
    assert _row(by_rel, "corrupt/not_an_image.png")["error"]
    assert _row(by_rel, "corrupt/empty.jpg")["error"]
    # header-only stub (Snapchat SCContent style) gets a plain-English reason,
    # not a raw "UnidentifiedImageError"
    hdr = _row(by_rel, "corrupt/header_only.png")["error"]
    assert hdr and "truncated png" in hdr.lower() and "Error" not in hdr
    # ... while the good video right next to it still worked
    assert _row(by_rel, "video/clip_ok.mp4")["thumb"]


def test_gpu_textures(processed):
    _case, _manifest, by_rel, _stats = processed
    assert _row(by_rel, "textures/uncompressed.ktx")["thumb"], \
        "uncompressed KTX should decode"
    assert _row(by_rel, "textures/apple_lzfse.ktx")["thumb"], \
        "Compression_APPLE / LZFSE KTX should decode"
    assert _row(by_rel, "textures/apple_snapshot.ktx")["thumb"], \
        "AAPL chunked LZFSE-ASTC snapshot should decode"
    assert _row(by_rel, "textures/apple_garbled.ktx")["error"]
    assert _row(by_rel, "textures/garbled.ktx")["error"]


def test_extensionless_and_lzc(processed):
    _case, _manifest, by_rel, _stats = processed
    # bare-hash-named images are content-sniffed and fully processed
    assert _row(by_rel, "noext/2f1a9c4b7e08d5")["kind"] == "image"
    assert _row(by_rel, "noext/2f1a9c4b7e08d5")["thumb"]
    assert _row(by_rel, "noext/8b30de55aa17f2")["thumb"]
    # a non-media extension-less file is not ingested at all
    assert "noext/not_media_a1b2c3" not in by_rel

    # LZC bundle with two images -> the larger one is what we thumbnail/hash
    lzc_img = _row(by_rel, "noext/lzc_images_9f8e7d")
    assert lzc_img["kind"] == "image" and lzc_img["thumb"] and lzc_img["phash"]
    # LZC bundle wrapping a video -> recognised and keyframed as video
    if "noext/lzc_video_1a2b3c" in by_rel:
        v = _row(by_rel, "noext/lzc_video_1a2b3c")
        assert v["kind"] == "video" and v["thumb"]
    # LZC bundle with no displayable media -> a clean error, no crash
    junk = _row(by_rel, "noext/lzc_junk_deadbeef")
    assert junk["error"] and "LZC" in junk["error"]


def test_heic_and_tiff_transcode_for_the_viewer(processed):
    from gleapp import imaging
    _case, _manifest, by_rel, _stats = processed
    for rel in ("heic/iphone_photo.heic", "images/scan.tiff",
                "textures/uncompressed.ktx"):
        src = _row(by_rel, rel)["path"]
        out = Path(src).with_suffix(".viewtest.jpg")
        assert imaging.transcode_isolated(src, out), f"{rel} did not transcode"
        assert out.stat().st_size > 200


def test_projectvic_roundtrip(collection, tmp_path_factory):
    media, _manifest = collection
    case = open_case(tmp_path_factory.mktemp("vcase") / "c", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(media / "projectvic.json")
        assert sources[0].kind == "projectvic"
        ingest_sources(case, sources)
        assert case.db.get_meta("vic_case_id") == "reg-1"
        rows = case.db.iter_files("media_id IS NOT NULL")
        assert rows and all(r["orig_name"] for r in rows)

        fid = rows[0]["id"]
        case.db.update_file(fid, category=2, notes="regression note")
        case.db.commit()

        out = media / "vic_out.json"
        projectvic.export_vic(case, out)
        doc = json.loads(out.read_text())
        m0 = next(m for m in doc["value"][0]["Media"]
                  if m["MediaID"] == rows[0]["media_id"])
        assert m0["Category"] == 2
        assert m0["Comments"] == "regression note"
    finally:
        case.close()


def test_search_covers_original_device_path(collection, tmp_path_factory):
    media, _manifest = collection
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path_factory.mktemp("s") / "c"),
                                      "name": "S"})
    cl.post("/api/case/ingest", json={"spec": str(media / "projectvic.json"),
                                      "options": {"screen": False, "keyframes": 2}})
    for _ in range(120):
        j = cl.get("/api/job").get_json()
        if not j["running"] and j["stage"] in ("done", "error"):
            break
        __import__("time").sleep(0.5)
    assert j["stage"] == "done"
    # RelativeFilePath is <rel>, the DCIM-style path is only in orig_path
    assert cl.get("/api/files?q=100TEST").get_json()["total"] > 0
    assert cl.get("/api/files?q=photo_gps").get_json()["total"] == 1
