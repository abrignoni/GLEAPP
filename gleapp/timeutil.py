"""Display-timezone helpers.

Filesystem / ingest timestamps are stored as epoch seconds (UTC).  The examiner
picks a display timezone (per case, defaulting to the app-wide setting, default
UTC); it is applied wherever an epoch timestamp is *shown* - the list view, the
details pane, the HTML/CSV report - with daylight-saving handled automatically
by the IANA tz database.

It is deliberately **not** applied to ``created_dt`` (Captured / EXIF): that is
the camera's own local wall-clock time as recorded in the file, and is shown
exactly as stored.
"""

from __future__ import annotations

import datetime as _dt

try:  # zoneinfo ships in 3.9+; the tz database comes from the `tzdata` package
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Curated shortlist for the picker: (IANA name, friendly label).  "UTC" first.
COMMON_ZONES: list[tuple[str, str]] = [
    ("UTC", "UTC"),
    ("America/New_York", "US Eastern"),
    ("America/Chicago", "US Central"),
    ("America/Denver", "US Mountain"),
    ("America/Phoenix", "US Arizona (no DST)"),
    ("America/Los_Angeles", "US Pacific"),
    ("America/Anchorage", "US Alaska"),
    ("Pacific/Honolulu", "US Hawaii (no DST)"),
    ("America/Halifax", "Atlantic"),
    ("America/Toronto", "Canada Eastern"),
    ("America/Sao_Paulo", "Brazil (Sao Paulo)"),
    ("Europe/London", "UK / Ireland / Portugal"),
    ("Europe/Paris", "Central Europe"),
    ("Europe/Athens", "Eastern Europe"),
    ("Africa/Johannesburg", "South Africa (no DST)"),
    ("Asia/Dubai", "Gulf (no DST)"),
    ("Asia/Kolkata", "India (no DST)"),
    ("Asia/Singapore", "Singapore / Malaysia (no DST)"),
    ("Asia/Tokyo", "Japan / Korea (no DST)"),
    ("Australia/Sydney", "Sydney / Melbourne"),
    ("Pacific/Auckland", "New Zealand"),
]


def resolve(name: str | None):
    """An IANA name -> a tzinfo; unknown / empty / 'UTC' -> UTC."""
    if not name or str(name).strip().upper() == "UTC" or ZoneInfo is None:
        return _dt.timezone.utc
    try:
        return ZoneInfo(str(name).strip())
    except Exception:  # noqa: BLE001 - bad name just falls back to UTC
        return _dt.timezone.utc


def is_known(name: str | None) -> bool:
    """True if ``name`` is 'UTC' or a resolvable IANA zone."""
    if not name or str(name).strip().upper() == "UTC":
        return True
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(str(name).strip())
        return True
    except Exception:  # noqa: BLE001
        return False


def fmt_epoch(v, name: str | None = None, *,
              fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Epoch seconds -> a string in the given display timezone, or '' ."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if not f:
        return ""
    try:
        return _dt.datetime.fromtimestamp(f, resolve(name)).strftime(fmt).strip()
    except (OverflowError, OSError, ValueError):
        return ""


def label(name: str | None) -> str:
    """e.g. 'America/New_York (UTC-04:00, EDT)' - the offset is 'now'."""
    z = resolve(name)
    now = _dt.datetime.now(z)
    raw = now.strftime("%z") or "+0000"
    off = f"{raw[:3]}:{raw[3:]}"
    disp = "UTC" if z is _dt.timezone.utc else str(name).strip()
    abbr = now.strftime("%Z")
    extra = f", {abbr}" if abbr and abbr not in ("UTC", disp) and not abbr[0].isdigit() else ""
    return f"{disp} (UTC{off}{extra})"
