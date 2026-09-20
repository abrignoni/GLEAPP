#!/usr/bin/env python3
"""Assemble the third-party notices a distributed build has to carry.

Nearly every permissive licence asks for the same thing and asks for it in the
binary, not only in the source tree: MIT wants its notice in "all copies or
substantial portions", BSD wants it "in the documentation and/or other materials
provided with the distribution", and the Apache 1.1 licence on the vendored
Impacket code wants a named acknowledgment in the end-user documentation. A frozen
build is a binary distribution, so all of it belongs inside the build.

PyInstaller does not do this by itself. It copies a package's dist-info only when a
hook happens to ask for metadata, so on one measured build 45 installed
distributions produced 14 dist-info folders, and GLEAPP's own LICENSE was not in the
bundle at all.

    python3 tools/make_notices.py                 # write NOTICES.txt at the repo root
    python3 tools/make_notices.py --check         # report gaps, write nothing
    python3 tools/make_notices.py -o somewhere.txt

packaging/gleapp.spec calls build_notices() with the distributions PyInstaller
actually bundled, so the file cannot credit something the build does not ship, nor
miss something it does.

A package that ships no licence text is reported, never skipped. Three did when this
was written (proxy_tools and two pyobjc frameworks), each declaring MIT in metadata
and shipping no file, and the entry says exactly that. A package with neither a text
nor a declared licence is the one case the build refuses, because then nothing is
known about it at all.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import sys
from pathlib import Path

# Installed so the build can run, never part of what it ships. Keeping this list
# short and explicit is deliberate: anything not named here is treated as shipped,
# so a new dependency lands in the notices rather than being silently dropped.
BUILD_ONLY = {
    "pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib",
    "altgraph", "macholib", "pefile", "pywin32-ctypes",
    "pytest", "iniconfig", "pluggy", "piexif", "pylint", "astroid",
    # the encoder that writes the HEIC test fixture; the app decodes with pi-heif
    # and packaging/gleapp.spec excludes this one from the build
    "pillow-heif",
}

# Licence texts that live in this repo rather than in a package. The vendored
# readers are MIT and Impacket is Apache 1.1, and all of them ask for the notice to
# travel with a binary copy.
REPO_NOTICES = [
    ("GLEAPP", "LICENSE"),
    ("Impacket (gleapp/vendor/impacket_ese.py)", "gleapp/vendor/LICENSE-impacket"),
    ("qnxprobe (gleapp/vendor/qnxprobe.py)", "gleapp/vendor/LICENSE-qnxprobe"),
    ("ewfprobe (gleapp/vendor/ewfprobe.py)", "gleapp/vendor/LICENSE-ewfprobe"),
    ("mediacarve (gleapp/vendor/mediacarve.py)", "gleapp/vendor/LICENSE-mediacarve"),
    ("SFace face-recognition model (gleapp/models)", "gleapp/models/LICENSE-sface"),
]

# Binaries a package carries without carrying their terms. pywebview bundles
# Microsoft's WebView2 SDK assemblies and ships only its own BSD licence, so the
# notice has to name them rather than let them travel unmentioned.
CARRIED_WITHOUT_TERMS = {
    "pywebview": (
        "pywebview bundles Microsoft's WebView2 SDK assemblies on Windows\n"
        "(Microsoft.Web.WebView2.Core.dll, Microsoft.Web.WebView2.WinForms.dll and\n"
        "WebView2Loader.dll) and ships no terms for them. They are covered by the\n"
        "Microsoft WebView2 SDK licence, not by pywebview's BSD licence below. The\n"
        "WebView2 Runtime itself is not bundled; see packaging/installer.iss."
    ),
}

_HEADER = """\
THIRD-PARTY NOTICES
===================

This build includes the software listed below. Each entry gives the licence the
package declares and, where the package ships one, its licence text in full.

GLEAPP's own licence is the first entry. Everything after it belongs to someone
else and is reproduced here because its licence asks for the notice to travel with
a binary copy.
"""

_RULE = "=" * 78


def _norm(name: str) -> str:
    return (name or "").strip().lower().replace("_", "-")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def declared_licence(dist: md.Distribution) -> str:
    """The licence a package states in metadata, as a name rather than a text."""
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [c.rsplit("::", 1)[-1].strip()
                   for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    # the free-text License field is often the whole licence, so keep the first line
    lic = (meta.get("License") or "").strip()
    return lic.splitlines()[0][:80] if lic else ""


def licence_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    """Every licence text a package ships, as (file name, text).

    Both places are searched. A package's dist-info carries what the wheel declared
    as licence files, and some packages instead keep them beside their code, which
    is where OpenCV puts the 179 KB third-party notice covering the FFmpeg stack it
    bundles. Missing either one would drop real notices.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for f in dist.files or []:
        name = str(f).rsplit("/", 1)[-1]
        upper = name.upper()
        in_dist_info = ".dist-info/" in str(f)
        looks_like = upper.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
        if not (looks_like or (in_dist_info and "/licenses/" in str(f))):
            continue
        if not looks_like and not name:
            continue
        try:
            path = Path(dist.locate_file(f))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            # a broken metadata entry must not stop the notices being written
            continue
        if not path.is_file():
            continue
        text = _read(path)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append((name, text))
    return sorted(out)


def _entry(dist: md.Distribution) -> dict:
    name = dist.metadata["Name"] or "?"
    return {
        "name": name,
        "version": dist.version or "?",
        "declared": declared_licence(dist),
        "texts": licence_texts(dist),
    }


def installed_entries(only: set[str] | None = None) -> list[dict]:
    """One entry per shipped distribution, skipping the build-only ones.

    ``only`` is the set of distribution names the build actually bundles. Without
    it every installed distribution is described, which over-reports rather than
    under-reports: a notices file listing something absent is untidy, one missing
    something present is the failure worth avoiding.
    """
    entries, seen = [], set()
    for dist in md.distributions():
        name = _norm(dist.metadata["Name"] or "")
        if not name or name in BUILD_ONLY or name in seen:
            continue
        if only is not None and name not in only:
            continue
        seen.add(name)
        entries.append(_entry(dist))
    return sorted(entries, key=lambda e: e["name"].lower())


def gaps(entries: list[dict]) -> list[str]:
    """Distributions nothing is known about: no licence text and no declaration.

    A package that ships no text but declares a licence is not a gap. Three did
    when this was written and the notice says so for each. A package with neither
    is the case the build refuses, because the notice would have nothing to say.
    """
    return sorted(e["name"] for e in entries if not e["texts"] and not e["declared"])


def build_notices(only: set[str] | None = None, root: Path | None = None) -> tuple[str, list[str]]:
    """The whole notices text, and the list of distributions nothing is known about."""
    root = root or Path(__file__).resolve().parent.parent
    entries = installed_entries(only)
    parts = [_HEADER]

    for label, rel in REPO_NOTICES:
        text = _read(root / rel)
        if text is None:
            continue
        parts.append(f"{_RULE}\n{label}\n{_RULE}\n\n{text}\n")

    for e in entries:
        head = f"{e['name']} {e['version']}"
        declared = f"Declared licence: {e['declared']}" if e["declared"] else \
                   "Declared licence: not stated by the package"
        block = [f"{_RULE}\n{head}\n{_RULE}\n", declared]
        note = CARRIED_WITHOUT_TERMS.get(_norm(e["name"]))
        if note:
            block.append("\n" + note)
        if e["texts"]:
            for fname, text in e["texts"]:
                block.append(f"\n--- {fname} ---\n{text}")
        else:
            block.append("\nThis package ships no licence text of its own; the licence it "
                         "declares is recorded above.")
        parts.append("\n".join(block) + "\n")

    return "\n".join(parts), gaps(entries)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", default="NOTICES.txt",
                    help="where to write (default: NOTICES.txt at the repo root)")
    ap.add_argument("--check", action="store_true",
                    help="report distributions nothing is known about, write nothing")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    text, missing = build_notices(root=root)
    entries = installed_entries()
    no_text = [e["name"] for e in entries if not e["texts"]]

    print(f"{len(entries)} shipped distribution(s)")
    print(f"  with a licence text: {len(entries) - len(no_text)}")
    if no_text:
        print(f"  declaring a licence but shipping no text: {', '.join(sorted(no_text))}")
    if missing:
        print(f"  NOTHING KNOWN (no text, no declaration): {', '.join(missing)}")

    if args.check:
        return 1 if missing else 0

    out = Path(args.output)
    if not out.is_absolute():
        out = root / out
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text):,} characters)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
