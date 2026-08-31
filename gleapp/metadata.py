"""EXIF / metadata extraction for images (and best-effort for video)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

_GPS_IFD = ExifTags.IFD.GPSInfo
_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
_GPSTAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}


def _to_deg(value: Any, ref: str | None) -> float | None:
    try:
        d, m, s = (float(x) for x in value)
    except (TypeError, ValueError):
        return None
    dec = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        dec = -dec
    return round(dec, 7)


def _parse_dt(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = str(raw).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            return _dt.datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return None


def extract_image(path: str | Path, img: Image.Image | None = None) -> dict[str, Any]:
    """Return width/height, capture time, GPS, camera string from an image."""
    out: dict[str, Any] = {
        "width": None, "height": None, "created_dt": None,
        "gps_lat": None, "gps_lon": None, "camera": None,
    }
    close = False
    if img is None:
        img = Image.open(path)
        close = True
    try:
        out["width"], out["height"] = img.size
        exif = img.getexif()
        if not exif:
            return out

        try:                       # DateTimeOriginal etc. live in the Exif sub-IFD
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
        except Exception:
            exif_ifd = {}

        def tag(name: str) -> Any:
            code = _TAGS.get(name, -1)
            return exif.get(code) if code in exif else exif_ifd.get(code)

        out["created_dt"] = (
            _parse_dt(tag("DateTimeOriginal"))
            or _parse_dt(tag("DateTimeDigitized"))
            or _parse_dt(tag("DateTime"))
        )
        make, model = tag("Make"), tag("Model")
        if make or model:
            out["camera"] = " ".join(
                str(x).strip() for x in (make, model) if x
            ).strip() or None

        try:
            gps = exif.get_ifd(_GPS_IFD)
        except Exception:
            gps = {}
        if gps:
            lat = gps.get(_GPSTAGS.get("GPSLatitude", -1))
            lat_ref = gps.get(_GPSTAGS.get("GPSLatitudeRef", -1))
            lon = gps.get(_GPSTAGS.get("GPSLongitude", -1))
            lon_ref = gps.get(_GPSTAGS.get("GPSLongitudeRef", -1))
            if lat and lon:
                out["gps_lat"] = _to_deg(lat, lat_ref)
                out["gps_lon"] = _to_deg(lon, lon_ref)
    except Exception:
        pass
    finally:
        if close:
            img.close()
    return out


def best_created_dt(exif_dt: str | None, mtime: float, ctime: float) -> str | None:
    """Fall back to filesystem times when EXIF has no capture date."""
    if exif_dt:
        return exif_dt
    ts = min(t for t in (mtime, ctime) if t) if (mtime or ctime) else None
    if ts:
        return _dt.datetime.fromtimestamp(ts).isoformat()
    return None
