"""Build GLEAPP for the machine this runs on, in two phases so a signed build is possible.

    python packaging/build.py exe                 phase 1: PyInstaller -> dist/GLEAPP/ (one-folder)
    python packaging/build.py exe --onefile       phase 1: one executable in dist/ (no installer from this)
    python packaging/build.py installer           phase 2: Windows -> dist/GLEAPP-Setup-<version>.exe (Inno Setup);
                                                  macOS -> dist/GLEAPP-<version>.dmg around dist/GLEAPP.app
    python packaging/build.py installer --sign-tool NAME
                                                  phase 2, with Inno Setup signing the installer and the
                                                  uninstaller using the Sign Tool configured under NAME
    python packaging/build.py all                 both phases in one go, for unsigned local builds
    python packaging/build.py verify PATH ...     assert a code signature is present and valid
                                                  (Authenticode on Windows, codesign on macOS);
                                                  --subject TEXT also requires the signer to match

Signing belongs between the two phases. Sign dist/GLEAPP/GLEAPP.exe after phase 1 and
before phase 2, or the installer carries an unsigned executable inside a signed wrapper.
`all` refuses --sign-tool for exactly that reason.

The version is read from gleapp/__init__.py as text, so nothing is imported, and passed
to Inno Setup, so the installer cannot report a different version from the app. ONEFILE
reaches the spec through the GLEAPP_ONEFILE environment variable; the spec file is never
edited by a build. PyInstaller is pinned in the [build] extra of pyproject.toml, which
phase 1 installs.

Phase 1 runs anywhere PyInstaller does and, on macOS, also produces GLEAPP.app. Phase 2
makes an Inno Setup installer on Windows and a .dmg on macOS; a Linux AppImage is not
wired up yet, and the command says so instead of producing nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
SPEC = PACKAGING / "gleapp.spec"
ISS = PACKAGING / "installer.iss"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP = "GLEAPP"
ISCC_DEFAULT = r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"


def read_version() -> str:
    """The version string from gleapp/__init__.py, read as text so nothing is imported."""
    text = (ROOT / "gleapp" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        sys.exit("build: could not find __version__ in gleapp/__init__.py")
    return m.group(1)


def exe_name() -> str:
    return f"{APP}.exe" if sys.platform == "win32" else APP


def run(cmd: list[str], **kwargs) -> None:
    print("==>", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False, **kwargs)
    if result.returncode != 0:
        sys.exit(f"build: '{cmd[0]}' exited {result.returncode}")


def assert_artifact(path: Path, what: str) -> None:
    """A build that exits 0 can still leave no usable output. Check the file, not the code."""
    if not path.is_file():
        sys.exit(f"build: {what} not produced at {path}")
    print(f"==> {what}: {path} ({path.stat().st_size:,} bytes)", flush=True)


def build_exe(onefile: bool, clean: bool) -> Path:
    if clean:
        for folder in (BUILD, DIST):
            print(f"==> removing {folder}", flush=True)
            shutil.rmtree(folder, ignore_errors=True)
    out = DIST / exe_name() if onefile else DIST / APP / exe_name()
    refuse_collision(onefile, out)
    run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "-e", f"{ROOT}[build]"])
    env = dict(os.environ, GLEAPP_ONEFILE="1" if onefile else "0")
    run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(BUILD)], env=env)
    assert_artifact(out, "executable")
    return out


def refuse_collision(onefile: bool, out: Path) -> None:
    """Stop a build from silently destroying the other layout's output.

    Off Windows both layouts are named dist/GLEAPP, a file for --onefile and a folder
    otherwise, and PyInstaller's --noconfirm removes whatever is already there without
    a word. Measured: a --onefile build on top of a one-folder build logged
    "Removing dir dist/GLEAPP" and replaced 1,257 files with one, exit 0. In the
    signing workflow that folder can be the signed build waiting for phase 2.
    --clean is the explicit way to discard it.
    """
    folder = DIST / APP
    if onefile and out.is_dir():
        sys.exit(f"build: {out} is an existing one-folder build and a --onefile build at "
                 "that name would delete it; pass --clean to discard it, or move it first")
    if not onefile and folder.is_file():
        sys.exit(f"build: {folder} is an existing one-file build sitting where the folder "
                 "build must go; pass --clean to discard it, or move it first")


def find_iscc() -> str:
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    if Path(ISCC_DEFAULT).is_file():
        return ISCC_DEFAULT
    sys.exit("build: Inno Setup (ISCC.exe) not found on PATH or at the default install "
             "location; get it from https://jrsoftware.org/isdl.php")


def build_installer(sign_tool: str | None) -> Path:
    if sys.platform == "win32":
        return _build_inno(sign_tool)
    if sys.platform == "darwin":
        if sign_tool:
            sys.exit("build: --sign-tool is an Inno Setup concept; on macOS sign the bundle "
                     "with codesign after 'exe', then run 'installer' for the disk image")
        return _build_dmg()
    sys.exit("build: packaging is wired up for Windows (Inno Setup) and macOS (.dmg); "
             "a Linux AppImage is not yet")


def _build_inno(sign_tool: str | None) -> Path:
    exe = DIST / APP / exe_name()
    if not exe.is_file():
        sys.exit(f"build: {exe} not found; run 'exe' first, the one-folder build rather than "
                 "--onefile, because the installer packages dist/GLEAPP/")
    version = read_version()
    cmd = [find_iscc(), f"/DAppVer={version}"]
    if sign_tool:
        cmd.append(f"/DSignToolName={sign_tool}")
    cmd.append(str(ISS))
    run(cmd)
    out = DIST / f"{APP}-Setup-{version}.exe"
    assert_artifact(out, "installer")
    return out


def _build_dmg() -> Path:
    """A compressed disk image around the .app the spec's BUNDLE step produced."""
    app = DIST / f"{APP}.app"
    if not app.is_dir():
        sys.exit(f"build: {app} not found; run 'exe' first, the one-folder build, which "
                 "produces the bundle on macOS")
    version = read_version()
    out = DIST / f"{APP}-{version}.dmg"
    run(["hdiutil", "create", "-volname", APP, "-srcfolder", str(app), "-ov",
         "-format", "UDZO", str(out)])
    run(["hdiutil", "verify", str(out)])
    assert_artifact(out, "disk image")
    return out


def _signature(path: Path) -> tuple[bool, str, bool, str]:
    """(valid, signer subject, timestamped, detail) from the platform's own verifier."""
    if sys.platform == "win32":
        literal = str(path).replace("'", "''")
        script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; "
                  "Write-Output $s.Status; "
                  "Write-Output ([string]$s.SignerCertificate.Subject); "
                  "Write-Output ([bool]$s.TimeStamperCertificate)")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, check=False)
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if result.returncode != 0 or len(lines) < 3:
            return False, "", False, f"could not read the signature: {result.stderr.strip()[:200]}"
        status, signer, stamped = lines[0], lines[1], lines[2].lower() == "true"
        return status == "Valid", signer, stamped, f"status={status} signer={signer!r}"
    if sys.platform == "darwin":
        result = subprocess.run(["codesign", "--verify", "--strict", "--verbose=2", str(path)],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False, "", False, f"codesign: {result.stderr.strip()[:200]}"
        detail = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)],
                                capture_output=True, text=True, check=False).stderr
        authorities = [ln.split("=", 1)[1] for ln in detail.splitlines() if ln.startswith("Authority=")]
        signer = authorities[0] if authorities else "(unknown)"
        stamped = any(ln.startswith("Timestamp=") for ln in detail.splitlines())
        return True, signer, stamped, ""
    return False, "", False, f"no signature verifier for platform {sys.platform}"


def verify(paths: list[str], subject: str | None) -> int:
    failures = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            print(f"FAIL  {path}: no such file")
            failures += 1
            continue
        valid, signer, stamped, detail = _signature(path)
        if not valid:
            print(f"FAIL  {path}  {detail}")
            failures += 1
            continue
        if subject and subject not in signer:
            print(f"FAIL  {path}  signer={signer!r} does not contain {subject!r}")
            failures += 1
            continue
        note = "" if stamped else "  (no timestamp: this signature stops verifying when the certificate expires)"
        print(f"OK    {path}  signer={signer}{note}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build.py", epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("exe", help="phase 1: build the executable with PyInstaller")
    p.add_argument("--onefile", action="store_true", help="one executable instead of one folder")
    p.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    p = sub.add_parser("installer", help="phase 2 (Windows): Inno Setup installer from dist/GLEAPP/")
    p.add_argument("--sign-tool", metavar="NAME",
                   help="Inno Setup Sign Tool name; signs the installer and the uninstaller")
    p = sub.add_parser("all", help="both phases, unsigned")
    p.add_argument("--onefile", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--sign-tool", metavar="NAME", help=argparse.SUPPRESS)
    p.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    p = sub.add_parser("verify", help="check code signatures on built files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--subject", metavar="TEXT", help="require the signer subject to contain TEXT")
    args = parser.parse_args(argv)

    if args.cmd == "all" and args.onefile:
        parser.error("the installer packages the one-folder layout, dist/GLEAPP/; "
                     "it cannot be built from a --onefile build")
    if args.cmd == "all" and args.sign_tool:
        parser.error("'all' builds unsigned; to sign, run 'exe', sign dist/GLEAPP/GLEAPP.exe, "
                     "then 'installer --sign-tool NAME'")

    if args.cmd == "exe":
        build_exe(args.onefile, args.clean)
    elif args.cmd == "installer":
        build_installer(args.sign_tool)
    elif args.cmd == "all":
        build_exe(False, args.clean)
        build_installer(None)
    elif args.cmd == "verify":
        return verify(args.paths, args.subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
