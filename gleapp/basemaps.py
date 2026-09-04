"""Offline basemaps for the gallery's map: import, list, describe, serve.

GLEAPP ships no map data. An examiner imports a basemap file after installation; GLEAPP
copies it under its data folder, records its SHA-256, and serves it to the gallery from
the local server, so the page loads nothing from anywhere else and the report can name
the exact file a view was drawn on. Two formats:

``.pmtiles``, the recommended one
    A single-file vector tileset (PMTiles v3; the specification is public domain). The
    gallery reads it by HTTP byte ranges through MapLibre GL JS and pmtiles.js, so GLEAPP
    serves the file as it is and never decodes a tile itself. A region is cut from the
    Protomaps planet build with the ``pmtiles`` command-line tool::

        pmtiles extract https://build.protomaps.com/<date>.pmtiles region.pmtiles --bbox=W,S,E,N

    Measured 2026-09-04 against the 137.7 GB build of 2026-09-02: the Washington DC
    metro bounding box came out at 28 MB in 8 s and Puerto Rico at 70 MB in 11 s, both
    zoom 0 to 15. The data is OpenStreetMap under the ODbL, distributed by Protomaps as a
    produced work, and the map shows the OpenStreetMap attribution. It is shown as plain
    text: an attribution link would be the one outbound address on the page.

``.mbtiles``, the raster fallback
    A SQLite file of pre-drawn image tiles, as QGIS, MapTiler Desktop and GIS shops
    produce. GLEAPP answers each tile with one query. MBTiles count rows from the bottom
    (TMS) and the web counts from the top (XYZ), so the row is flipped. Vector MBTiles are
    refused: they would need a second style, second fonts and second sprites.

Style layers, fonts and sprites for the vector basemap are vendored under
``gleapp/web/static/maps`` (see the README there).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import struct
import time
from pathlib import Path

from . import appconfig

FORMAT_PMTILES = "pmtiles"
FORMAT_MBTILES = "mbtiles"
_EXT = {FORMAT_PMTILES: ".pmtiles", FORMAT_MBTILES: ".mbtiles"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK = 1 << 20
_PMTILES_MAGIC = b"PMTiles"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_TILE_TYPES = {0: "unknown", 1: "mvt", 2: "png", 3: "jpg", 4: "webp", 5: "avif", 6: "mlt"}
_COMPRESSION = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
EXAMINER_ATTRIBUTION = "Basemap supplied by the examiner"


def basemap_dir() -> Path:
    d = appconfig.data_dir() / "basemaps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


# ---- reading the files ------------------------------------------------------
def read_pmtiles_header(path: str | Path) -> dict:
    """The fixed 127-byte PMTiles v3 header plus the JSON metadata, as a dict.

    Field offsets follow the v3 specification (protomaps/PMTiles, spec/v3/spec.md).
    Metadata compressed with brotli or zstd is left unread; the header alone is enough
    to place the map.
    """
    with open(path, "rb") as fh:
        head = fh.read(127)
        if len(head) < 127 or head[:7] != _PMTILES_MAGIC:
            raise ValueError("not a PMTiles file")
        version = head[7]
        if version != 3:
            raise ValueError(f"PMTiles version {version} is not supported (only v3)")
        (root_off, root_len, meta_off, meta_len, leaf_off, leaf_len, tile_off, tile_len,
         addressed, entries, contents) = struct.unpack_from("<11Q", head, 8)
        clustered, internal_comp, tile_comp, tile_type, min_zoom, max_zoom = head[96:102]
        min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<4i", head, 102)
        center_zoom = head[118]
        center_lon, center_lat = struct.unpack_from("<2i", head, 119)
        metadata: dict = {}
        if meta_len and meta_len < 50 * 1024 * 1024:
            fh.seek(meta_off)
            raw = fh.read(meta_len)
            try:
                if internal_comp == 2:
                    raw = gzip.decompress(raw)
                elif internal_comp not in (0, 1):
                    raw = b""
                if raw:
                    metadata = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError, EOFError):
                metadata = {}
    return {
        "spec_version": version,
        "tile_type": _TILE_TYPES.get(tile_type, str(tile_type)),
        "tile_compression": _COMPRESSION.get(tile_comp, str(tile_comp)),
        "internal_compression": _COMPRESSION.get(internal_comp, str(internal_comp)),
        "clustered": bool(clustered),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "bounds": [min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7],
        "center": [center_lon / 1e7, center_lat / 1e7, center_zoom],
        "addressed_tiles": addressed,
        "tile_entries": entries,
        "tile_contents": contents,
        "offsets": {"root": [root_off, root_len], "metadata": [meta_off, meta_len],
                    "leaf": [leaf_off, leaf_len], "tiles": [tile_off, tile_len]},
        "metadata": {k: metadata[k] for k in ("name", "description", "attribution",
                                                "version", "type") if k in metadata},
    }


def mbtiles_info(path: str | Path) -> dict:
    """The metadata table of a raster MBTiles, with the tile format checked."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        meta = {r[0]: r[1] for r in conn.execute("SELECT name, value FROM metadata")}
        fmt = str(meta.get("format", "")).lower()
        if not fmt:
            row = conn.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
            blob = row[0] if row else b""
            if blob[:8] == b"\x89PNG\r\n\x1a\n":
                fmt = "png"
            elif blob[:3] == b"\xff\xd8\xff":
                fmt = "jpg"
            elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                fmt = "webp"
        if fmt == "pbf":
            raise ValueError("vector MBTiles are not supported; import a .pmtiles basemap")
        if fmt not in _MIME:
            raise ValueError(f"MBTiles tile format {fmt or 'unknown'!r} is not an image format")
        zooms = conn.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
    finally:
        conn.close()

    def num(key, default):
        try:
            return float(meta[key])
        except (KeyError, TypeError, ValueError):
            return default

    bounds = None
    if meta.get("bounds"):
        try:
            bounds = [float(v) for v in str(meta["bounds"]).split(",")]
            if len(bounds) != 4:
                bounds = None
        except ValueError:
            bounds = None
    return {
        "tile_type": fmt,
        "min_zoom": int(num("minzoom", zooms[0] if zooms and zooms[0] is not None else 0)),
        "max_zoom": int(num("maxzoom", zooms[1] if zooms and zooms[1] is not None else 0)),
        "bounds": bounds or [-180.0, -85.0511, 180.0, 85.0511],
        "metadata": {k: meta[k] for k in ("name", "description", "attribution", "version",
                                           "type") if k in meta},
    }


def inspect(path: str | Path) -> dict:
    """``{"format": ..., **info}`` for a basemap file, decided by its bytes."""
    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(16)
    if head[:7] == _PMTILES_MAGIC:
        return {"format": FORMAT_PMTILES, **read_pmtiles_header(p)}
    if head == _SQLITE_MAGIC:
        return {"format": FORMAT_MBTILES, **mbtiles_info(p)}
    raise ValueError(f"{p.name} is neither a PMTiles nor an MBTiles file")


def mbtiles_tile(path: str | Path, z: int, x: int, y: int) -> tuple[bytes, str] | None:
    """One raster tile addressed the XYZ way, or None if the file has none there."""
    row = (1 << z) - 1 - y                     # MBTiles stores TMS rows
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        got = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, row)).fetchone()
        if not got:
            return None
        blob = bytes(got[0])
        fmt = conn.execute("SELECT value FROM metadata WHERE name='format'").fetchone()
    finally:
        conn.close()
    kind = str(fmt[0]).lower() if fmt and fmt[0] else ""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        kind = "png"
    elif blob[:3] == b"\xff\xd8\xff":
        kind = "jpg"
    elif blob[:4] == b"RIFF":
        kind = "webp"
    return blob, _MIME.get(kind, "application/octet-stream")


def _pm_uvarint(buf: bytes, pos: int):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _zxy_to_tile_id(z: int, x: int, y: int) -> int:
    """The PMTiles v3 tile id for z/x/y: the per-zoom base plus the Hilbert index."""
    acc = ((1 << (2 * z)) - 1) // 3               # sum of 4**t for t in 0..z-1
    n = 1 << z
    d = 0
    rx = ry = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def _decode_pmtiles_dir(buf: bytes) -> list[tuple[int, int, int, int]]:
    """A decompressed PMTiles directory into (tile_id, offset, length, run_length)."""
    pos = 0
    count, pos = _pm_uvarint(buf, pos)
    ids = [0] * count
    last = 0
    for i in range(count):
        v, pos = _pm_uvarint(buf, pos)
        last += v
        ids[i] = last
    runs = [0] * count
    for i in range(count):
        runs[i], pos = _pm_uvarint(buf, pos)
    lens = [0] * count
    for i in range(count):
        lens[i], pos = _pm_uvarint(buf, pos)
    offs = [0] * count
    for i in range(count):
        v, pos = _pm_uvarint(buf, pos)
        offs[i] = offs[i - 1] + lens[i - 1] if v == 0 and i > 0 else v - 1
    return list(zip(ids, offs, lens, runs))


def _find_entry(entries: list[tuple[int, int, int, int]], tid: int):
    """The directory entry whose run covers ``tid`` (bisect on tile_id), or None."""
    lo, hi = 0, len(entries) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if entries[mid][0] <= tid:
            found = entries[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if found is None:
        return None
    tile_id, _off, _ln, run = found
    if run == 0:                                   # a leaf-directory pointer
        return found
    return found if tid < tile_id + run else None


def _pm_decompress(raw: bytes, name: str) -> bytes | None:
    if name in ("none", "unknown"):
        return raw
    if name == "gzip":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return None
    return None                                    # brotli / zstd not bundled


def pmtiles_tile(path, z: int, x: int, y: int) -> bytes | None:
    """The decompressed bytes of one tile (MVT for a vector map, image bytes for a
    raster PMTiles), or None if the archive has no tile at z/x/y.

    Reads the root directory and, when needed, one leaf directory, exactly as the
    PMTiles v3 reader does, and never fetches anything: the file is opened
    read-only and only the byte ranges a lookup needs are read.
    """
    head = read_pmtiles_header(path)
    if not (head["min_zoom"] <= z <= head["max_zoom"]):
        return None
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):            # z/x/y off the grid: not a tile
        return None
    root_off, root_len = head["offsets"]["root"]
    leaf_off, _leaf_len = head["offsets"]["leaf"]
    tile_off, _tile_len = head["offsets"]["tiles"]
    dir_comp = head["internal_compression"]
    tid = _zxy_to_tile_id(z, x, y)
    with open(path, "rb") as fh:
        fh.seek(root_off)
        root = _pm_decompress(fh.read(root_len), dir_comp)
        if root is None:
            return None
        entries = _decode_pmtiles_dir(root)
        entry = _find_entry(entries, tid)
        if entry and entry[3] == 0:                # descend into the leaf directory
            _tid, off, ln, _run = entry
            fh.seek(leaf_off + off)
            leaf = _pm_decompress(fh.read(ln), dir_comp)
            if leaf is None:
                return None
            entry = _find_entry(_decode_pmtiles_dir(leaf), tid)
        if not entry or entry[3] == 0:
            return None
        _tid, off, ln, _run = entry
        fh.seek(tile_off + off)
        return _pm_decompress(fh.read(ln), head["tile_compression"])


# ---- the store --------------------------------------------------------------
def _sidecar(name: str) -> Path:
    return basemap_dir() / f"{name}.json"


def _safe_name(text: str) -> str:
    stem = Path(text).stem if "." in Path(text).name else text
    return _SAFE.sub("-", stem).strip("-.")[:60] or "basemap"


def _record(name: str) -> dict | None:
    try:
        rec = json.loads(_sidecar(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rec["path"] = str(basemap_dir() / rec["file"])
    return rec


def get_active() -> str | None:
    name = appconfig.load().get("basemap")
    return name if name and _record(name) else None


def set_active(name: str | None) -> None:
    cfg = appconfig.load()
    if name:
        if _record(name) is None:
            raise ValueError(f"no basemap named {name!r}")
        cfg["basemap"] = name
    else:
        cfg.pop("basemap", None)
    appconfig.save(cfg)


def list_basemaps() -> list[dict]:
    active = get_active()
    out = []
    for sc in sorted(basemap_dir().glob("*.json")):
        rec = _record(sc.stem)
        if rec is None:
            continue
        rec["present"] = Path(rec["path"]).is_file()
        rec["active"] = rec["name"] == active
        out.append(rec)
    return out


def get(name: str) -> dict | None:
    return _record(name) if name else None


def import_basemap(src: str | Path, *, name: str | None = None, progress=None) -> dict:
    """Copy a basemap file under the data folder, hashing it in the same pass, and
    describe it. ``progress(copied_bytes, total_bytes)`` is called every chunk. The
    first basemap imported becomes the active one."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"{src} is not a file")
    info = inspect(src)
    base = _safe_name(name or src.name)
    final = base
    n = 2
    while _sidecar(final).exists():
        final = f"{base}-{n}"
        n += 1
    ext = _EXT[info["format"]]
    dest = basemap_dir() / f"{final}{ext}"
    total = src.stat().st_size
    h = hashlib.sha256()
    done = 0
    part = dest.with_name(dest.name + ".part")
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while chunk := fin.read(_CHUNK):
                fout.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    attribution = _strip_tags(str(info.get("metadata", {}).get("attribution", "")))
    if "openstreetmap" in attribution.lower():
        # Protomaps builds carry an HTML attribution link; the ODbL wants the credit
        # shown, not a way off the machine, so the text form is what the map displays.
        attribution = OSM_ATTRIBUTION
    elif not attribution:
        # a file that names no source is credited to whoever brought it, not guessed at
        attribution = EXAMINER_ATTRIBUTION
    rec = {
        "name": final,
        "file": dest.name,
        "format": info["format"],
        "size": total,
        "sha256": h.hexdigest(),
        "imported_at": time.time(),
        "source_name": src.name,
        "tile_type": info["tile_type"],
        "min_zoom": info["min_zoom"],
        "max_zoom": info["max_zoom"],
        "bounds": info["bounds"],
        "attribution": attribution,
        "metadata": info.get("metadata", {}),
    }
    _sidecar(final).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    if get_active() is None:
        set_active(final)
    rec["path"] = str(dest)
    return rec


def remove_basemap(name: str) -> bool:
    rec = _record(name)
    if rec is None:
        return False
    Path(rec["path"]).unlink(missing_ok=True)
    _sidecar(name).unlink(missing_ok=True)
    if appconfig.load().get("basemap") == name:
        set_active(None)
    return True


# ---- the map style ----------------------------------------------------------
_STATIC = Path(__file__).parent / "web" / "static" / "maps"


def style(name: str, *, flavor: str = "dark", url_prefix: str = "/basemap",
          asset_prefix: str = "/static/maps") -> dict:
    """A MapLibre style for one basemap, everything on the local server."""
    rec = _record(name)
    if rec is None:
        raise ValueError(f"no basemap named {name!r}")
    flavor = flavor if flavor in ("dark", "light") else "dark"
    if rec["format"] == FORMAT_PMTILES:
        layers = json.loads((_STATIC / f"layers-{flavor}.json").read_text(encoding="utf-8"))
        return {
            "version": 8,
            "name": f"{rec['name']} ({flavor})",
            "glyphs": f"{asset_prefix}/fonts/{{fontstack}}/{{range}}.pbf",
            "sprite": f"{asset_prefix}/sprites/v4/{flavor}",
            "sources": {"basemap": {
                "type": "vector",
                "url": f"pmtiles://{url_prefix}/{rec['file']}",
                "attribution": rec["attribution"],
            }},
            "layers": layers,
        }
    return {
        "version": 8,
        "name": f"{rec['name']} (raster)",
        "sources": {"basemap": {
            "type": "raster",
            "tiles": [f"{url_prefix}/{rec['name']}/{{z}}/{{x}}/{{y}}"],
            "tileSize": 256,
            "minzoom": rec["min_zoom"],
            "maxzoom": rec["max_zoom"],
            "bounds": rec["bounds"],
            "attribution": rec["attribution"],
        }},
        "layers": [
            {"id": "background", "type": "background",
             "paint": {"background-color": "#1b1e24" if flavor == "dark" else "#e8e8e8"}},
            {"id": "basemap", "type": "raster", "source": "basemap"},
        ],
    }


# ---- the case's record of what it was viewed on -----------------------------
def record_use(case, name: str) -> dict | None:
    """Note on the case which basemap its map was drawn on, so the report can name it.
    Written once per basemap; returns the record."""
    rec = _record(name)
    if rec is None:
        return None
    if case.db.get_meta("basemap_sha256") != rec["sha256"]:
        case.db.set_meta("basemap_name", rec["name"])
        case.db.set_meta("basemap_sha256", rec["sha256"])
        case.db.set_meta("basemap_format", rec["format"])
        case.db.audit_log(case.examiner, "basemap",
                          f"map drawn on {rec['name']} ({rec['format']}, sha256 {rec['sha256']})")
    return rec
