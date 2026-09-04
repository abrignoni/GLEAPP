"""Render a static map image from an imported offline basemap, for the report.

Given a basemap and one or more (lon, lat) points, this draws a locator image
with a marker on each point and returns a PNG. It reads only the local basemap
file and fetches nothing, so a report built with it stays self-contained and
offline, on every platform, with Pillow as the only dependency.

A raster basemap (.mbtiles, or a raster .pmtiles) is drawn by compositing its
image tiles. A vector basemap (.pmtiles, the recommended kind) is drawn by
decoding its Mapbox Vector Tiles and filling land, water, land use and buildings
and stroking roads and boundaries in a flat, print-friendly palette. Labels are
not drawn: the image is a locator, not a substitute for the interactive map.
"""

from __future__ import annotations

import io
import math

from . import basemaps, mvt

TILE = 256
MIME = {"png": "image/png", "jpeg": "image/jpeg"}
_SS = 2                                            # supersample, then downscale for smooth edges

# Which MVT layers to paint, in draw order, and how. "fill" fills polygons;
# "line" strokes paths (roads also get a wider casing underneath).
_LAYER_STYLE = [
    ("earth", "fill", "earth"),
    ("landcover", "fill", "landuse"),
    ("landuse", "fill", "landuse"),
    ("water", "fill", "water"),
    ("buildings", "fill", "buildings"),
    ("boundaries", "line", "boundary"),
    ("roads", "road", "road"),
]

FLAVORS = {
    "light": {
        "bg": (242, 239, 233), "earth": (242, 239, 233), "landuse": (223, 233, 208),
        "water": (167, 201, 229), "buildings": (225, 219, 205),
        "road_casing": (211, 200, 180), "road": (255, 255, 255), "boundary": (188, 170, 190),
        "marker": (228, 72, 60), "marker_edge": (255, 255, 255), "frame": (176, 168, 156),
    },
    "dark": {
        "bg": (30, 30, 27), "earth": (37, 37, 33), "landuse": (37, 47, 30),
        "water": (22, 32, 43), "buildings": (44, 44, 40),
        "road_casing": (58, 58, 51), "road": (92, 90, 82), "boundary": (76, 70, 82),
        "marker": (228, 72, 60), "marker_edge": (245, 245, 245), "frame": (70, 70, 64),
    },
}


def _world_px(lon: float, lat: float, z: float) -> tuple[float, float]:
    """Web-Mercator world pixel of a coordinate at zoom ``z`` (256 px tiles)."""
    scale = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * scale
    siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
    return x, y


def _fit_zoom(points, width, height, zmin, zmax) -> int:
    """The highest integer zoom at which every point fits in the frame with margin."""
    if len(points) < 2:
        return max(zmin, min(zmax, 12))
    pad = 0.82
    for z in range(zmax, zmin - 1, -1):
        xs, ys = zip(*(_world_px(lon, lat, z) for lon, lat in points))
        if (max(xs) - min(xs)) <= width * pad and (max(ys) - min(ys)) <= height * pad:
            return z
    return zmin


def _tile(rec: dict, z: int, tx: int, ty: int, cache: dict):
    """A tile as ``("mvt", layers)`` or ``("img", png_bytes)``, cached; None if absent."""
    key = (rec["path"], z, tx, ty)
    if key in cache:
        return cache[key]
    val = None
    fmt = rec.get("format")
    if fmt == basemaps.FORMAT_PMTILES:
        raw = basemaps.pmtiles_tile(rec["path"], z, tx, ty)
        if raw is not None:
            val = ("mvt", mvt.decode(raw)) if rec.get("tile_type") == "mvt" else ("img", raw)
    elif fmt == basemaps.FORMAT_MBTILES:
        got = basemaps.mbtiles_tile(rec["path"], z, tx, ty)
        if got is not None:
            val = ("img", got[0])
    cache[key] = val
    return val


def _paint_vector(draw, layers, ox, oy, pal):
    """Draw one decoded MVT tile; ``ox, oy`` is its top-left in canvas pixels."""
    for name, kind, color_key in _LAYER_STYLE:
        layer = layers.get(name)
        if not layer:
            continue
        scale = TILE * _SS / layer["extent"]
        color = pal[color_key]
        for feat in layer["features"]:
            for path in feat["paths"]:
                pts = [(ox + px * scale, oy + py * scale) for px, py in path]
                if kind == "fill":
                    if len(pts) >= 3:
                        draw.polygon(pts, fill=color)
                elif kind == "road":
                    if len(pts) >= 2:
                        draw.line(pts, fill=pal["road_casing"], width=4 * _SS // 2 + 1,
                                  joint="curve")
                        draw.line(pts, fill=color, width=2 * _SS // 2, joint="curve")
                else:                              # line (boundaries)
                    if len(pts) >= 2:
                        draw.line(pts, fill=color, width=_SS, joint="curve")


def render(rec: dict, points, *, width: int = 480, height: int = 320,
           zoom: int | None = None, flavor: str = "light", cache: dict | None = None,
           fmt: str = "png") -> bytes:
    """A PNG locator image of ``points`` (a list of (lon, lat)) on ``rec``.

    A single point is centred; several points are framed to fit them all. Reads
    only the local basemap file. ``cache`` (a dict) is reused across calls in one
    report so shared tiles are decoded once.
    """
    from PIL import Image, ImageDraw

    pal = FLAVORS.get(flavor, FLAVORS["light"])
    cache = cache if cache is not None else {}
    zmin = int(rec.get("min_zoom", 0) or 0)
    zmax = int(rec.get("max_zoom", 19) or 19)
    pts = [(float(lon), float(lat)) for lon, lat in points if lon is not None and lat is not None]
    if not pts:
        pts = [(0.0, 0.0)]

    if zoom is not None:
        z = max(zmin, min(zmax, zoom))
    elif len(pts) == 1:
        z = min(zmax, max(zmin, 15))
    else:
        z = _fit_zoom(pts, width, height, zmin, zmax)

    wpx = [_world_px(lon, lat, z) for lon, lat in pts]
    cx = (min(x for x, _ in wpx) + max(x for x, _ in wpx)) / 2
    cy = (min(y for _, y in wpx) + max(y for _, y in wpx)) / 2

    W, H = width * _SS, height * _SS
    left = cx * _SS - W / 2
    top = cy * _SS - H / 2                          # canvas origin in supersampled world px
    canvas = Image.new("RGB", (W, H), pal["bg"])
    draw = ImageDraw.Draw(canvas)

    n = 1 << z
    tx0 = math.floor((left / _SS) / TILE)
    tx1 = math.floor((left / _SS + W / _SS) / TILE)
    ty0 = max(0, math.floor((top / _SS) / TILE))
    ty1 = min(n - 1, math.floor((top / _SS + H / _SS) / TILE))
    for ty in range(ty0, ty1 + 1):
        for txx in range(tx0, tx1 + 1):
            tx = txx % n
            got = _tile(rec, z, tx, ty, cache)
            if not got:
                continue
            ox = txx * TILE * _SS - left            # tile top-left on the canvas
            oy = ty * TILE * _SS - top
            kind, payload = got
            if kind == "mvt":
                _paint_vector(draw, payload, ox, oy, pal)
            else:
                try:
                    with Image.open(io.BytesIO(payload)) as tim:
                        tim = tim.convert("RGB")
                        if tim.size != (TILE * _SS, TILE * _SS):
                            tim = tim.resize((TILE * _SS, TILE * _SS),
                                              Image.BILINEAR)  # pylint: disable=no-member
                    canvas.paste(tim, (int(round(ox)), int(round(oy))))
                except Exception:  # pylint: disable=broad-exception-caught
                    continue                        # a bad tile is just skipped

    r = 7 * _SS
    for lon, lat in pts:
        wx, wy = _world_px(lon, lat, z)
        sx, sy = wx * _SS - left, wy * _SS - top
        draw.ellipse((sx - r - _SS, sy - r - _SS, sx + r + _SS, sy + r + _SS),
                     fill=pal["marker_edge"])
        draw.ellipse((sx - r, sy - r, sx + r, sy + r), fill=pal["marker"],
                     outline=pal["marker_edge"], width=_SS)

    out = canvas.resize((width, height), Image.LANCZOS)  # pylint: disable=no-member
    ImageDraw.Draw(out).rectangle((0, 0, width - 1, height - 1), outline=pal["frame"])
    buf = io.BytesIO()
    if fmt == "jpeg":
        out.save(buf, "JPEG", quality=82)
    else:
        out.save(buf, "PNG", optimize=True)
    return buf.getvalue()
