"""Render a static map image from an imported offline basemap, for the report.

Given a basemap and one or more (lon, lat) points, this draws a locator image
with a marker on each point and returns a PNG. It reads only the local basemap
file and fetches nothing, so a report built with it stays self-contained and
offline, on every platform, with Pillow as the only dependency.

A raster basemap (.mbtiles, or a raster .pmtiles) is drawn by compositing its
image tiles. A vector basemap (.pmtiles, the recommended kind) is drawn by
decoding its Mapbox Vector Tiles and filling land, water, land use and buildings
and stroking roads and boundaries in a flat, print-friendly palette, with the place,
water and street names its tiles carry drawn over that so a locator says where it is.
Labels need a scalable font from Pillow; a build without one draws the map with no
labels rather than failing.
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
        "label": (60, 56, 50), "label_halo": (250, 249, 245),
    },
    "dark": {
        "bg": (30, 30, 27), "earth": (37, 37, 33), "landuse": (37, 47, 30),
        "water": (22, 32, 43), "buildings": (44, 44, 40),
        "road_casing": (58, 58, 51), "road": (92, 90, 82), "boundary": (76, 70, 82),
        "marker": (228, 72, 60), "marker_edge": (245, 245, 245), "frame": (70, 70, 64),
        "label": (222, 219, 210), "label_halo": (24, 24, 21),
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


# Which layers carry names worth drawing, in the order they are placed: the ones
# that orient a reader first, then the streets. Each entry is
# (layer, final-pixel font size, whether the geometry is a point).
_LABEL_LAYERS = [
    ("places", 12, True),
    ("water", 11, True),
    ("roads", 10, False),
]


def _font(size: int):
    """A scalable font, or None where this build cannot provide one.

    Pillow's own default is used rather than a font vendored here: it needs no new
    asset or licence and it is present on every platform the tool ships to. A build
    without FreeType, or a Pillow older than the scalable default, simply draws no
    labels, which is why every caller treats None as "skip the labels" rather than
    as an error.
    """
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError, ImportError):
        return None


def _path_midpoint(path):
    """The point half way along a polyline, by length.

    Taking the middle vertex instead puts the label at an end of a straight road,
    since a two-point line has no middle vertex, and a road drawn with its detail
    bunched at one end would be labelled off to that side.
    """
    lengths = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(lengths)
    if total <= 0:
        return path[0]
    half = total / 2
    for i, seg in enumerate(lengths):
        if half <= seg:
            t = half / seg if seg else 0.0
            (x0, y0), (x1, y1) = path[i], path[i + 1]
            return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        half -= seg
    return path[-1]


def _label_points(layers, ox: float, oy: float, z: int):
    """Label candidates from one decoded tile as ``(order, size, x, y, text)``.

    A feature is offered only when the basemap itself says it should be visible at
    this zoom: every Protomaps feature carries ``min_zoom``, so the tileset's own
    guidance decides what appears rather than a rule invented here.
    """
    out = []
    for order, (layer_name, size, is_point) in enumerate(_LABEL_LAYERS):
        layer = layers.get(layer_name)
        if not layer:
            continue
        scale = TILE * _SS / layer["extent"]
        for feat in layer["features"]:
            tags = feat.get("tags") or {}
            text = tags.get("name")
            if not text or not isinstance(text, str):
                continue
            min_zoom = tags.get("min_zoom")
            if isinstance(min_zoom, (int, float)) and min_zoom > z:
                continue
            paths = feat.get("paths") or []
            if not paths:
                continue
            if is_point:
                px, py = paths[0][0]
            else:
                # the longest path of the feature, labelled at its middle, so a road
                # that only clips the corner of the tile is not labelled there
                path = max(paths, key=len)
                if len(path) < 2:
                    continue
                px, py = _path_midpoint(path)
            out.append((order, size, ox + px * scale, oy + py * scale, text))
    return out


def _draw_labels(draw, candidates, pal, width: int, height: int) -> int:
    """Draw as many labels as fit without overlapping. Returns how many were drawn.

    One label per name: a street crossing four tiles is one street, and repeating it
    is noise rather than information.
    """
    fonts: dict[int, object] = {}
    placed: list[tuple[float, float, float, float]] = []
    seen: set[str] = set()
    drawn = 0
    for _order, size, x, y, text in sorted(candidates, key=lambda c: (c[0], c[4])):
        if text in seen:
            continue
        px = size * _SS
        if px not in fonts:
            fonts[px] = _font(px)
        font = fonts[px]
        if font is None:
            return 0                               # no font: draw none rather than some
        try:
            box = draw.textbbox((x, y), text, font=font, anchor="mm")
        except (ValueError, OSError):
            continue
        pad = 2 * _SS
        box = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
        if box[0] < 0 or box[1] < 0 or box[2] > width or box[3] > height:
            continue                                # would be clipped at the edge
        if any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3]
               for o in placed):
            continue                                # would sit on a label already drawn
        draw.text((x, y), text, font=font, fill=pal["label"], anchor="mm",
                  stroke_width=max(1, _SS), stroke_fill=pal["label_halo"])
        placed.append(box)
        seen.add(text)
        drawn += 1
    return drawn


def covers(rec: dict, lon: float, lat: float, *, zoom: int | None = None) -> bool:
    """Whether the basemap actually holds a tile at this coordinate.

    A point outside a regional basemap still renders: the tiles come back empty and
    the result is the background colour with a pin on it, which reads as a location
    with nothing around it. Measured on the Orlando basemap, a Stockholm point drew
    an image that was 98.3% one colour against 13.0% for a point inside it. So a
    caller that wants a map only where there is one asks here first.

    This reads the archive rather than the bounds it declares, because an MBTiles
    with no bounds in its metadata is taken to cover the whole world.
    """
    zmin = int(rec.get("min_zoom", 0) or 0)
    zmax = int(rec.get("max_zoom", 19) or 19)
    # the zoom render() uses for a single point, so this probes the tile it will draw
    z = max(zmin, min(zmax, 15 if zoom is None else zoom))
    try:
        x, y = _world_px(float(lon), float(lat), z)
    except (TypeError, ValueError):
        return False
    return _tile(rec, z, int(x // TILE), int(y // TILE), {}) is not None


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
           fmt: str = "png", labels: bool = True) -> bytes:
    """A PNG locator image of ``points`` (a list of (lon, lat)) on ``rec``.

    A single point is centred; several points are framed to fit them all. Reads
    only the local basemap file. ``cache`` (a dict) is reused across calls in one
    report so shared tiles are decoded once.

    ``labels`` draws the street, water and place names a vector basemap carries, so
    a locator says where it is rather than only showing a shape. A raster basemap
    has its labels baked into its tiles already and is unaffected.
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

    label_points: list = []
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
                if labels:
                    label_points += _label_points(payload, ox, oy, z)
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

    if labels and label_points:
        _draw_labels(draw, label_points, pal, W, H)

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
