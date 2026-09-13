"""Per-user application config (recent cases, window state).

Stored outside any case so the desktop app can offer "recent cases" on launch.
Location: ``$GLEAPP_CONFIG_DIR`` if set, else %APPDATA%\\GLEAPP on Windows /
~/.config/gleapp elsewhere.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

APP_NAME = "GLEAPP"


def config_dir() -> Path:
    override = os.environ.get("GLEAPP_CONFIG_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    """Large app data (the global hash store) - kept off a roaming profile.

    ``$GLEAPP_CONFIG_DIR`` still wins (tests point it at a tmp dir); otherwise
    %LOCALAPPDATA%\\GLEAPP on Windows, same as ``config_dir`` elsewhere.
    """
    override = os.environ.get("GLEAPP_CONFIG_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "win32":
        d = Path(os.environ.get("LOCALAPPDATA",
                                Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg_path() -> Path:
    return config_dir() / "config.json"


def load() -> dict:
    try:
        return json.loads(_cfg_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> None:
    try:
        _cfg_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def get_timezone() -> str:
    """The examiner's chosen display timezone (IANA name), or 'UTC'."""
    return load().get("timezone") or "UTC"


def set_timezone(name: str | None) -> None:
    cfg = load()
    if name and str(name).strip().upper() != "UTC":
        cfg["timezone"] = str(name).strip()
    else:
        cfg.pop("timezone", None)
    save(cfg)


def _case_summary(case_dir: Path) -> dict | None:
    """(files, name) for a case dir, or None if it isn't a real GLEAPP case."""
    db = case_dir / "case.gleapp"
    if not db.exists():
        return None
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            row = conn.execute(
                "SELECT value FROM meta WHERE key='case_name'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {"files": int(files), "name": row[0] if row and row[0] else None}


def recent_cases(*, include_empty: bool = False, limit: int | None = None) -> list[dict]:
    """Recent cases that still exist on disk, newest first.

    Each entry is annotated with a live ``files`` count and the case's own
    stored name.  Directories without a ``case.gleapp`` are dropped, and
    empty (0-file) cases are hidden unless ``include_empty`` is set - an
    abandoned freshly-created case must never be the thing a click lands on.
    ``limit``, when given, caps the number of entries returned (the newest
    ``limit`` survivors) without disturbing what is stored on disk.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for i in load().get("recent", []):
        if limit is not None and len(out) >= limit:
            break
        p = Path(i.get("path", ""))
        key = str(p).lower()
        if key in seen:
            continue
        summary = _case_summary(p)
        if summary is None:
            continue
        if summary["files"] == 0 and not include_empty:
            continue
        seen.add(key)
        out.append({
            "path": str(p),
            "name": summary["name"] or i.get("name") or p.name,
            "opened": i.get("opened", 0),
            "files": summary["files"],
        })
    return out


def clear_recent() -> None:
    """Forget every recent case. Nothing on disk is touched - this only
    empties the launcher's own memory of what it has opened before."""
    cfg = load()
    cfg["recent"] = []
    save(cfg)


def push_recent(path: str, name: str | None = None) -> None:
    cfg = load()
    path = str(Path(path).resolve())
    recent = [i for i in cfg.get("recent", [])
              if str(Path(i.get("path", ""))).lower() != path.lower()]
    recent.insert(0, {"path": path, "name": name or Path(path).name,
                      "opened": time.time()})
    cfg["recent"] = recent[:12]
    save(cfg)
