"""Build Linux .deb and .rpm packages from the frozen one-folder build.

    python packaging/linux/build_linux_packages.py            both packages
    python packaging/linux/build_linux_packages.py --deb-only
    python packaging/linux/build_linux_packages.py --rpm-only

The input is phase 1's output, ``dist/GLEAPP/`` (see packaging/build.py): the
packages wrap the frozen binary rather than the Python sources, so they carry
the exact dependency versions the release was built and smoke-tested with
(OpenCV 5, which the system packages of most distributions cannot provide)
and work offline. The install layout is the same everywhere:

    /opt/gleapp/            the frozen tree (GLEAPP executable beside _internal/)
    /usr/bin/gleapp         symlink to /opt/gleapp/GLEAPP
    /usr/share/applications/gleapp.desktop
    /usr/share/icons/...    the logo as SVG (scalable) and PNG (256px)

Options: --dist (default dist/GLEAPP), --out-dir (default dist),
--work-dir (default: a temporary directory), --release (RPM release,
default 1). The version is read from gleapp/__init__.py as text, the same
way packaging/build.py reads it, so the packages cannot disagree with the
app. Needs dpkg-deb for the .deb and rpmbuild for the .rpm.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DIST_DEFAULT = ROOT / "dist" / "GLEAPP"
DESKTOP = ROOT / "packaging" / "linux" / "gleapp.desktop"
SVG_ICON = ROOT / "docs" / "images" / "gleapp-logo.svg"
PNG_ICON = ROOT / "docs" / "images" / "gleapp-logo.png"
LICENSE = ROOT / "LICENSE"

SUMMARY = "Media forensics triage toolkit"
DESCRIPTION = (
    "GLEAPP ingests folders of images and video (or a JSON job file), hashes and "
    "de-duplicates them, pulls metadata, extracts video key frames, runs lightweight "
    "visual screening, matches against known-hash lists, and serves a fast local "
    "review gallery with HTML, CSV, JSON and KML reports."
)
MAINTAINER = "GLEAPP contributors <noreply@github.com>"
HOMEPAGE = "https://github.com/abrignoni/GLEAPP"
# SPDX, the same set pyproject.toml declares for the wheel.
LICENSE_SPDX = "MIT AND Apache-1.1 AND Apache-2.0 AND BSD-3-Clause AND OFL-1.1 AND CC0-1.0"


def read_version() -> str:
    """The version string from gleapp/__init__.py, read as text like build.py."""
    text = (ROOT / "gleapp" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        sys.exit("build_linux_packages: could not find __version__ in gleapp/__init__.py")
    return m.group(1)


def check_dist(dist: Path) -> Path:
    """The frozen binary inside the one-folder build; refuse anything else."""
    exe = dist / "GLEAPP"
    if not dist.is_dir() or not exe.is_file():
        sys.exit(f"build_linux_packages: {exe} not found; run "
                 "'python packaging/build.py exe' first (the one-folder build, "
                 "not --onefile)")
    return exe


def run(cmd: list[str]) -> None:
    print("==>", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(f"build_linux_packages: '{cmd[0]}' exited {result.returncode}")


def need_tool(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"build_linux_packages: {name} not found; {hint}")


def install_tree(dist: Path, root: Path) -> None:
    """Copy the frozen tree and the desktop integration into staging root."""
    for src in (DESKTOP, SVG_ICON, PNG_ICON, LICENSE):
        if not src.is_file():
            sys.exit(f"build_linux_packages: required file missing: {src}")
    opt = root / "opt" / "gleapp"
    shutil.copytree(dist, opt)
    (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "usr" / "bin" / "gleapp").symlink_to("/opt/gleapp/GLEAPP")
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    shutil.copy(DESKTOP, apps / "gleapp.desktop")
    svg_dir = root / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    svg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(SVG_ICON, svg_dir / "gleapp.svg")
    png_dir = root / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
    png_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(PNG_ICON, png_dir / "gleapp.png")
    doc = root / "usr" / "share" / "doc" / "gleapp"
    doc.mkdir(parents=True, exist_ok=True)
    shutil.copy(LICENSE, doc / "copyright")
    # PyInstaller 6 collects data files under _internal/ (measured in the
    # v2026.5.7 release tarball), older layouts at the bundle root.
    notices = next((p for p in (opt / "NOTICES.txt", opt / "_internal" / "NOTICES.txt")
                    if p.is_file()), None)
    if notices is not None:
        shutil.copy(notices, doc / "NOTICES.txt")


def dir_size_kb(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return (total + 1023) // 1024


def build_deb(dist: Path, out_dir: Path, version: str, work: Path) -> Path:
    need_tool("dpkg-deb", "on Debian/Ubuntu: sudo apt-get install dpkg-dev")
    root = work / "deb-root"
    if root.exists():
        shutil.rmtree(root)
    install_tree(dist, root)
    debian = root / "DEBIAN"
    debian.mkdir(parents=True)
    (debian / "control").write_text(
        f"Package: gleapp\n"
        f"Version: {version}\n"
        f"Architecture: amd64\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Section: utils\n"
        f"Priority: optional\n"
        f"Homepage: {HOMEPAGE}\n"
        f"Installed-Size: {dir_size_kb(root)}\n"
        f"Recommends: xdg-utils\n"
        f"Description: {SUMMARY}\n"
        f" {DESCRIPTION}\n",
        encoding="utf-8",
    )
    hook = ("#!/bin/sh\nset -e\n"
            "update-desktop-database /usr/share/applications >/dev/null 2>&1 || true\n"
            "gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true\n"
            "exit 0\n")
    (debian / "postinst").write_text(hook, encoding="utf-8")
    (debian / "postrm").write_text(hook, encoding="utf-8")
    (debian / "postinst").chmod(0o755)
    (debian / "postrm").chmod(0o755)
    out = out_dir / f"gleapp_{version}_amd64.deb"
    if out.exists():
        out.unlink()
    run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(out)])
    if not out.is_file():
        sys.exit(f"build_linux_packages: .deb not produced at {out}")
    print(f"==> {out} ({out.stat().st_size:,} bytes)", flush=True)
    run(["dpkg-deb", "--field", str(out)])
    return out


def spec_text(version: str, release: str, payload: Path) -> str:
    return f"""Name: gleapp
Version: {version}
Release: {release}%{{?dist}}
Summary: {SUMMARY}
License: {LICENSE_SPDX}
URL: {HOMEPAGE}
BuildArch: x86_64
# The payload is the frozen PyInstaller tree: every library it needs travels
# inside /opt/gleapp, so automatic dependency detection would only restate the
# bundled copies. The one host requirement is the C library the README names
# (glibc 2.39 or newer, the Ubuntu 24.04 the release is built on).
AutoReqProv: no
Requires: glibc

%description
{DESCRIPTION}

%install
cp -a "{payload}/"* "%{{buildroot}}/"

%post
update-desktop-database /usr/share/applications >/dev/null 2>&1 || :
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || :
exit 0

%postun
update-desktop-database /usr/share/applications >/dev/null 2>&1 || :
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || :
exit 0

%files
/opt/gleapp/
/usr/bin/gleapp
/usr/share/applications/gleapp.desktop
/usr/share/icons/hicolor/scalable/apps/gleapp.svg
/usr/share/icons/hicolor/256x256/apps/gleapp.png
/usr/share/doc/gleapp/
"""


def build_rpm(dist: Path, out_dir: Path, version: str, release: str, work: Path) -> Path:
    need_tool("rpmbuild", "on Debian/Ubuntu: sudo apt-get install rpm; "
              "on Fedora/RHEL: sudo dnf install rpm-build")
    topdir = work / "rpm-topdir"
    for sub in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (topdir / sub).mkdir(parents=True, exist_ok=True)
    payload = topdir / "BUILD" / "gleapp-root"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    install_tree(dist, payload)
    (topdir / "SOURCES" / "gleapp.spec").write_text(spec_text(version, release, payload),
                                                   encoding="utf-8")
    run(["rpmbuild", "-bb", "--define", f"_topdir {topdir}",
         "--buildroot", str(topdir / "BUILDROOT"),
         str(topdir / "SOURCES" / "gleapp.spec")])
    rpms = sorted((topdir / "RPMS").rglob("*.rpm"))
    if not rpms:
        sys.exit("build_linux_packages: rpmbuild produced no .rpm")
    out = out_dir / f"gleapp-{version}-{release}.x86_64.rpm"
    if out.exists():
        out.unlink()
    shutil.move(str(rpms[0]), out)
    print(f"==> {out} ({out.stat().st_size:,} bytes)", flush=True)
    run(["rpm", "-qpi", str(out)])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_linux_packages.py",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist", default=str(DIST_DEFAULT),
                    help="the frozen one-folder build (default: dist/GLEAPP)")
    ap.add_argument("--out-dir", default=str(ROOT / "dist"),
                    help="where to write the packages (default: dist/)")
    ap.add_argument("--work-dir", default=None,
                    help="staging area (default: a temporary directory)")
    ap.add_argument("--release", default="1", help="RPM release (default: 1)")
    ap.add_argument("--deb-only", action="store_true")
    ap.add_argument("--rpm-only", action="store_true")
    args = ap.parse_args(argv)

    version = read_version()
    if not re.match(r"^[0-9]", version):
        sys.exit(f"build_linux_packages: version {version!r} cannot start a "
                 "Debian/RPM version")
    dist = Path(args.dist)
    check_dist(dist)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp = None
    if args.work_dir:
        work = Path(args.work_dir)
        work.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="gleapp-linux-pkg-")
        work = Path(tmp.name)

    if not args.rpm_only:
        build_deb(dist, out_dir, version, work)
    if not args.deb_only:
        build_rpm(dist, out_dir, version, args.release, work)
    if tmp is not None:
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
