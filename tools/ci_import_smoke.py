"""Import every module in the gleapp package and report the ones that fail.

This checks importability, not behaviour. Its value is platform coverage: GLEAPP is
built and shipped for Windows, while most development happens elsewhere, and a whole
class of breakage only shows up on the other OS. Path separators, case-sensitivity
assumptions, a module that reaches for a POSIX-only name at import time, and a
conditional import that resolves on one platform and not the other are all invisible
to a test suite that never runs there.

Modules are imported one at a time so a single failure names itself instead of
aborting the sweep, and the exit status counts every failure rather than the first.

Usage:
    python tools/ci_import_smoke.py
"""

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

PACKAGE = "gleapp"

# Running `python tools/ci_import_smoke.py` puts tools/ on sys.path, not the repo root,
# so resolve the root from this file rather than relying on the caller's PYTHONPATH or
# working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def discover():
    """Return the package plus every submodule under it, in a stable order."""
    package = importlib.import_module(PACKAGE)
    names = [PACKAGE]
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{PACKAGE}."):
        names.append(info.name)
    return sorted(set(names))


def main():
    try:
        names = discover()
    except Exception:  # pylint: disable=broad-exception-caught
        print(f"could not import the {PACKAGE} package at all:", file=sys.stderr)
        traceback.print_exc()
        return 2

    # A discovery step that finds nothing would otherwise pass silently, reporting a
    # clean sweep of zero modules.
    if len(names) < 2:
        print(f"discovered only {len(names)} module(s) under {PACKAGE}; expected the "
              f"whole package. Treating as a failure rather than a pass.", file=sys.stderr)
        return 2

    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    print(f"Importing {len(names)} modules under {PACKAGE}/\n")

    failed = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:  # pylint: disable=broad-exception-caught
            failed.append(name)
            print(f"  FAIL  {name}")
            traceback.print_exc()
            print()
        else:
            print(f"  ok    {name}")

    print()
    if failed:
        print(f"{len(failed)} of {len(names)} modules failed to import:")
        for name in failed:
            print(f"  {name}")
        return 1

    print(f"All {len(names)} modules imported cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
